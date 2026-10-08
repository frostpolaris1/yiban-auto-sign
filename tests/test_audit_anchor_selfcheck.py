# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""审计锚点自检的活体反例：把输入改坏，工具必须响。

被整改的缺陷是"判据在自动层面等于没有"：锚点文件只判"行数变少/相等"两支，
**仅追加 1 条垃圾行**就让两道判据同时返回"无异常"；非法 UTF-8 抛
`UnicodeDecodeError`，被 web 每日线程的兜底 except 吞成一条 WARNING ⇒ 当日校验
整体不执行；无密钥即可伪造被采信的 v2 锚点行（`prev_line_hash` 是无密钥 sha256，
其余字段是库内明文副本）。

本文件按"每道自检必须自带一条把输入改坏 ⇒ 工具必须红"的活体反例纪律，逐条钉死
修复后的判据：行数三支（变少/相等/**变多**）、解析失败判"无法定论"（≠无异常也≠
普通红）、校验基准取 `max_id` 最大真行、库内指纹与锚点旁路文件自报的**两方**一致性。

标签：G · 安全：脱敏/审计/配置注入
覆盖：`_anchor_file_state_ex` 的行数三支与不可解析行、`_anchor_status` 的四态结论
（ok/none/tampered/indeterminate）、`audit_health` 对"无法定论"的汇总与事实清单呈现、
`record_audit_anchor` 对不可解析文件的拒绝续写、三字段（v0）历史锚点行的兼容与
兼容不得放掉篡改检测、以及 `scripts/audit_verify.py` 对"无法定论"的 exit 2。
对应实现：`yiban/store/audit_chain.py`（`_anchor_status` / `_anchor_file_state_ex` /
`audit_health`）、`web/services/notify_mail.py` 的 `_audit_alert_facts`、
`scripts/audit_verify.py`。
关键断言：**"无法定论"必须与"无异常"和"确证篡改"两个结论都不同**——混进任一侧，
要么让一条非法字节把当日校验静默掉，要么把编码事故当失陷去响应；"变多"与"变少"
同等判红（正常写入每加一行都会抬高库内指纹高水位）。
依赖：临时库 + 临时 `.env` + 临时锚点文件；CLI 用例起真子进程，故依赖
`sys.executable` 并对子进程 stdout 按本地代码页解码；无网络、无 skip。

> 批 6c3-D（D-2）：行数判据四支（变多/相等/变少/count 不符）并一条参数化
> `test_anchor_judge_branches`；「读不出」两支（非法 UTF-8 / 无指纹的坏行）并一条
> `test_corrupt_sources_are_indeterminate_not_healthy`；每支 subTest 保留原断言与消息。
> 本类还承接 `test_audit_anchor.py` 下沉的健康往返与 `audit_health` 聚合断言。
"""
import contextlib
import importlib.util
import json
import locale
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64
AUDIT_KEY = "b" * 64


class _Fixture(unittest.TestCase):
    """临时库 + 临时锚点文件（互不干扰）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-anchor-selfcheck-")
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
        self._reset_state()

    def _reset_state(self):
        """重造夹具状态（6c3-D：并参数化后的同一用例内多支场景各需一次）。"""
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                os.remove(self.db_file + suffix)
        self.anchor = os.path.join(self.tmp, "audit-anchor.log")
        with contextlib.suppress(OSError):
            os.remove(self.anchor)
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

    def _lines(self):
        with open(self.anchor, encoding="utf-8") as f:
            return [ln.strip() for ln in f if ln.strip()]

    def _write_lines(self, lines):
        with open(self.anchor, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def _health(self):
        return db.audit_health(self.anchor)

    def _set_meta(self, lines, last_hash):
        payload = json.dumps({"lines": lines, "last_hash": last_hash, "ts": "x"})
        self._raw("INSERT OR REPLACE INTO app_meta (key, value) VALUES ('audit_anchor_meta', ?)",
                  (payload,))

    def _row_hash(self, row_id):
        with db._conn_lock:
            conn = db.get_conn()
            r = conn.execute("SELECT hash FROM audit_logs WHERE id=?", (row_id,)).fetchone()
        return (r["hash"] or "") if r else None


class HealthyBaselineTest(_Fixture):
    """健康基线：真锚点 + 真库 ⇒ healthy=True。"""

    def test_clean_state_is_healthy(self):
        self._seed(6)
        db.record_audit_anchor(self.anchor)
        h = self._health()
        self.assertEqual(h["anchor_status"], "ok", h["anchor_msg"])
        self.assertTrue(h["anchor_ok"])
        self.assertTrue(h["healthy"])
        self.assertNotIn("anchor_witness", h)
        # 快乐基线还承接两处原 test_audit_anchor.py 的汇总断言（6c3-D D-2 下沉）：
        # ① 记录/校验往返：verify_audit_anchor 通过且无话可说；② audit_health 聚合口径。
        ok, msg = db.verify_audit_anchor(self.anchor)
        self.assertTrue(ok, msg)
        self.assertEqual(msg, "")
        self.assertTrue(h["chain_ok"])
        self.assertEqual(h["write_failures"], 0)


class ThreeBranchJudgeTest(_Fixture):
    """行数判据与自报对账四支：变多 / 相等（正例）/ 变少 / count 不符（6c3-D D-2 并参数化）。

    四支原逐条同构（造一个锚点文件状态 → 断 anchor_status/anchor_ok/healthy 与消息
    点名），每支的造局手法与断言逐条保留为 subTest。
    """

    def _scenario_growth(self):
        """只追加 1 条垃圾行——旧实现两道判据同时"无异常"，现在必须判红。"""
        self._seed(4)
        db.record_audit_anchor(self.anchor)
        with open(self.anchor, "a", encoding="utf-8") as f:
            f.write("这一行是人为写坏的垃圾内容\n")
        h = self._health()
        self.assertEqual(h["anchor_status"], "tampered", h["anchor_msg"])
        self.assertFalse(h["anchor_ok"])
        self.assertFalse(h["healthy"])
        self.assertIn("增至", h["anchor_msg"], "必须点名「变多」这一支")

    def _scenario_valid_append(self):
        """正例对照：应用自己追加一行（库内高水位同步抬高）不得误报。"""
        self._seed(2)
        db.record_audit_anchor(self.anchor)
        self._seed(2)
        db.record_audit_anchor(self.anchor)
        h = self._health()
        self.assertEqual(h["anchor_status"], "ok", h["anchor_msg"])
        self.assertTrue(h["healthy"], h["anchor_msg"])

    def _scenario_truncation(self):
        self._seed(3)
        db.record_audit_anchor(self.anchor)
        self._seed(3)
        db.record_audit_anchor(self.anchor)
        self._write_lines(self._lines()[:1])
        h = self._health()
        self.assertEqual(h["anchor_status"], "tampered")
        self.assertIn("减至", h["anchor_msg"])

    def _scenario_count_mismatch(self):
        """锚点自报与库内真值不一致（声称 count=2，链内实有 6 行）必须判红。"""
        self._seed(6)
        db.record_audit_anchor(self.anchor)
        lines = self._lines()
        parts = lines[-1].split()
        parts[-4] = "2"           # count 字段
        lines[-1] = " ".join(parts)
        self._write_lines(lines)
        # 攻击者同时把库内指纹的末行哈希改成新值，制造"两边自洽"
        self._set_meta(len(lines), db._anchor_line_sha(lines[-1]))
        h = self._health()
        self.assertEqual(h["anchor_status"], "tampered", h["anchor_msg"])
        self.assertIn("不符", h["anchor_msg"])

    def test_anchor_judge_branches(self):
        for label, scenario in (
            ("变多：垃圾行追加即红", self._scenario_growth),
            ("相等：应用合法追加不误报", self._scenario_valid_append),
            ("变少：截断即红", self._scenario_truncation),
            ("自报 count 与库内不符即红", self._scenario_count_mismatch),
        ):
            with self.subTest(branch=label):
                self._reset_state()
                scenario()


class IndeterminateStateTest(_Fixture):
    """解析失败 = "无法定论"：≠无异常、≠普通红，且 healthy=False。"""

    def _scenario_invalid_utf8(self):
        self._seed(4)
        db.record_audit_anchor(self.anchor)
        with open(self.anchor, "ab") as f:
            f.write(b"\xff\xfe not utf-8 \x80\n")
        lines, status = db._read_anchor_lines_ex(self.anchor)
        self.assertIsNone(lines)
        self.assertEqual(status, "decode-error")
        h = self._health()
        self.assertEqual(h["anchor_status"], "indeterminate", h["anchor_msg"])
        self.assertFalse(h["anchor_ok"], "无法定论绝不能被当成通过")
        self.assertFalse(h["healthy"])
        ok, msg = db.verify_audit_anchor(self.anchor)
        self.assertFalse(ok)
        self.assertTrue(msg.startswith("无法定论："), msg)

    def _scenario_unparseable_without_meta(self):
        """无库内指纹可比时，结构坏行仍必须是"无法定论"而不是"无异常"。"""
        self._seed(2)
        with open(self.anchor, "w", encoding="utf-8") as f:
            f.write("完全是垃圾的一行\n")
        status, _msg = db._anchor_status(self.anchor)
        self.assertEqual(status, "indeterminate")
        self.assertFalse(self._health()["healthy"])

    def test_corrupt_sources_are_indeterminate_not_healthy(self):
        """「读不出」两类来源必须落同一结论：非法 UTF-8 与无指纹可比的结构坏行。

        （两条原逐条同构，6c3-D D-2 并参数化；每支的断言与消费点原样保留。）
        """
        for label, scenario in (
            ("非法 UTF-8 字节", self._scenario_invalid_utf8),
            ("无指纹可比的结构坏行", self._scenario_unparseable_without_meta),
        ):
            with self.subTest(source=label):
                self._reset_state()
                scenario()

    def test_alert_facts_name_indeterminate(self):
        self._seed(4)
        db.record_audit_anchor(self.anchor)
        with open(self.anchor, "ab") as f:
            f.write(b"\xff\xfe\n")
        webapp = _load_webapp("anchor_selfcheck_facts")
        facts = dict(webapp._audit_alert_facts(self._health()))
        self.assertIn("无法定论", facts["库外锚点"])
        self.assertIn("无法定论", facts["锚点说明"])
        self.assertNotIn("锚点独立见证", facts)

    def test_record_anchor_refuses_to_append_onto_broken_file(self):
        """读不出的锚点文件不得续写：按空列表续写会把行间链接到假前驱并掩盖现场。"""
        self._seed(3)
        db.record_audit_anchor(self.anchor)
        with open(self.anchor, "ab") as f:
            f.write(b"\xff\xfe\n")
        before = os.path.getsize(self.anchor)
        self.assertIsNone(db.record_audit_anchor(self.anchor))
        self.assertEqual(os.path.getsize(self.anchor), before, "拒绝续写必须是零写入")


class AnchorTwoPartyJudgeTest(_Fixture):
    """两方一致性（库内指纹 / 锚点旁路文件自报）任一不一致 ⇒ 红。"""

    def test_forged_v2_anchor_line_is_rejected(self):
        """无密钥伪造 v2 锚点行（自报一个库内不存在的 max_id）必须被拦下。"""
        self._seed(6)
        db.record_audit_anchor(self.anchor)
        lines = self._lines()
        prev = db._anchor_line_sha(lines[-1])
        forged = f"2026-01-01 00:00:00 1 999 999 0 {'f' * 64} {prev}"
        self.assertNotEqual(forged, lines[-1])
        lines.append(forged)
        self._write_lines(lines)
        # 攻击者把库内指纹也改成"两边自洽"（无密钥可算 sha256）
        self._set_meta(len(lines), db._anchor_line_sha(forged))
        h = self._health()
        self.assertEqual(h["anchor_status"], "tampered", h["anchor_msg"])
        self.assertIn("不存在", h["anchor_msg"])

    def test_max_anchor_baseline_ignores_smaller_trailing_claim(self):
        """基准取 max_id 最大真行：尾部再来一条更小 max_id 的"自洽"旧记录不算数。"""
        self._seed(6)
        db.record_audit_anchor(self.anchor)
        lines = self._lines()
        prev = db._anchor_line_sha(lines[-1])
        small = f"2026-01-01 00:00:00 1 2 2 0 {self._row_hash(2)} {prev}"
        lines.append(small)
        self._write_lines(lines)
        self._set_meta(len(lines), db._anchor_line_sha(small))
        picked = db._max_anchor_of(self._lines())
        self.assertEqual(picked["max_id"], 6, "必须选 max_id 最大的真行，而不是最后一行")

    def test_max_anchor_tie_takes_the_later_line(self):
        """max_id 并列取靠后（最新自报）：取靠前会拿陈旧 head 定点、误报篡改。

        清理后补锚、链尾重签后重锚都会追加 max_id 相同的新行，此时只有最新一行
        描述库内现状；`max()` 并列返回首个，与"并列取靠后的"意图相反。
        """
        self._seed(2)
        db.record_audit_anchor(self.anchor)
        self._raw("UPDATE audit_logs SET hash=? WHERE id=2", ("e" * 64,))
        db.record_audit_anchor(self.anchor)
        lines = self._lines()
        self.assertEqual(len(lines), 2, "前提：重锚须真正追加一行")
        self.assertEqual(lines[0].split()[3], lines[1].split()[3], "前提：两行 max_id 并列")
        picked = db._max_anchor_of(lines)
        self.assertEqual(picked["head"], "e" * 64,
                         "并列必须取靠后（最新自报），取靠前会拿陈旧 head 定点")


class LegacyThreeFieldAnchorTest(_Fixture):
    """三字段（v0）历史锚点行：必须解析，且兼容不得放掉篡改检测。

    生产实证（2026-09-29 起连续 10 天）：`/var/log/yiban/audit-anchor.log` 前 41 行是
    早期版本写入的 `<ts> <head>` 三字段行（日期 2026-08-21 → 2026-08-28）。解析器只认
    5/8 token，故 `_anchor_file_state_ex` 在第 1 行即返回 `indeterminate`（"第 1 行不是
    合法锚点行"），行数比对与行间链校验从未执行。每日自检实际已死 10 天，只因
    "结论与上次同态、不重复外发"而无人察觉。

    v0 行没有 min_id / max_id / count / purge_total / prev_line_hash，一律返回 None
    （不填 0 或空串），于是两条定性必须记住：
      * v0 行不能充当定点基准（`_max_anchor_of`）——它没有 max_id，且 None 参与
        `>=` 比较会抛 TypeError，把每日体检炸成异常；
      * v0 行仍须作为行间链的**前驱**参与哈希（与 v1 行同法）。链的覆盖面只到
        "后继行带 prev_line_hash" 的那些行：v0 与 v1 行都没有该字段，故它们
        **彼此之间**的相邻关系本就不受链保护，这在 v1 时代就已如此。本次兼容既没
        缩小、也没扩大覆盖面；老前缀的完整性由行数三支与末行哈希承担。
    """

    def _legacy_file(self):
        """复刻生产文件形状：三字段前缀 + v1 行 + v2 主体；库内指纹按总行数登记。"""
        self._seed(4)
        v0a = f"2026-08-21 00:00:31 {self._row_hash(1)}"
        v0b = f"2026-08-22 00:00:31 {self._row_hash(2)}"
        v1 = f"2026-08-28 00:00:31 1 3 {self._row_hash(3)}"
        v2 = "2026-09-01 00:00:31 1 4 4 0 {} {}".format(
            self._row_hash(4), db._anchor_line_sha(v1))
        lines = [v0a, v0b, v1, v2]
        self._write_lines(lines)
        self._set_meta(len(lines), db._anchor_line_sha(v2))
        return lines

    def test_legacy_prefix_file_is_ok(self):
        """三字段前缀不得让整份锚点判"无法定论"——生产上就是这个结论压了 10 天。"""
        lines = self._legacy_file()
        parsed = db._parse_anchor_lines(lines)
        self.assertEqual(len(parsed), len(lines), "每一行都必须可解析")
        self.assertEqual(parsed[0]["version"], 0)
        self.assertEqual(parsed[0]["head"], self._row_hash(1))
        for field in ("min_id", "max_id", "count", "purge_total", "prev_line_hash"):
            self.assertIsNone(parsed[0][field],
                              f"v0 行没有 {field}：必须是 None，不是 0 或空串")
        status, msg = db._anchor_status(self.anchor)
        self.assertEqual(status, "ok", msg)
        h = self._health()
        self.assertEqual(h["anchor_status"], "ok", h["anchor_msg"])
        self.assertTrue(h["healthy"], h["anchor_msg"])

    def test_legacy_row_is_chain_predecessor(self):
        """链的覆盖面：只在「后继行带 prev_line_hash」处开火。

        两段证据。① 后继（v2 行）带该字段时，改写三字段前驱行必红——老行确实在链上。
        ② 两条 v1 行相邻时，改写前一行不红——前一行没有带该字段的后继去哈希它。
        第②段是 v1 时代就有的覆盖面，不是本次兼容引入的缺口；本用例把这份真实覆盖面
        钉住，将来谁扩大了覆盖面，这里会红。
        """
        self._seed(3)
        v0 = f"2026-08-21 00:00:31 {self._row_hash(1)}"
        v2 = "2026-08-28 00:00:31 1 3 3 0 {} {}".format(
            self._row_hash(3), db._anchor_line_sha(v0))
        self._write_lines([v0, v2])
        self._set_meta(2, db._anchor_line_sha(v2))
        status, msg = db._anchor_status(self.anchor)
        self.assertEqual(status, "ok", msg)
        rewritten = f"2026-08-21 00:00:31 {'d' * 64}"
        self.assertEqual(len(rewritten.split()), 3, "前提：改写后仍是三字段行")
        self._write_lines([rewritten, v2])
        status, msg = db._anchor_status(self.anchor)
        self.assertEqual(status, "tampered", msg)
        self.assertIn("行间哈希不符", msg)

        # ② 两条 v1 行相邻：改写前一行，末行哈希仍对得上，故判 ok（覆盖面边界）
        v1a = f"2026-08-28 00:00:31 1 2 {self._row_hash(2)}"
        v1b = f"2026-08-29 00:00:31 1 3 {self._row_hash(3)}"
        self._write_lines([v1a, v1b])
        self._set_meta(2, db._anchor_line_sha(v1b))
        self.assertEqual(db._anchor_status(self.anchor)[0], "ok")
        self._write_lines([f"2026-08-28 00:00:31 1 2 {'e' * 64}", v1b])
        self.assertEqual(db._anchor_status(self.anchor)[0], "ok",
                         "前一行没有带 prev_line_hash 的后继：链本来就够不到它")

    def test_only_legacy_rows_is_indeterminate_not_healthy(self):
        """文件里只有三字段行：没有 max_id 可定点 ⇒ 无法定论，且不得抛异常。"""
        self._seed(2)
        self._write_lines([f"2026-08-21 00:00:31 {self._row_hash(2)}"])
        status, msg = db._anchor_status(self.anchor)  # None 参与 max_id 比较会 TypeError
        self.assertEqual(status, "indeterminate", msg)
        self.assertIn("max_id", msg)
        self.assertFalse(self._health()["healthy"])

    def test_tampering_with_legacy_prefix_is_still_detected(self):
        """兼容老格式不得放掉篡改检测：追加行、截断末行、改写末行都须判红。"""
        scenarios = (
            ("追加一行三字段行", lambda ls: [*ls, f"2026-08-29 00:00:31 {'c' * 64}"]),
            ("追加一行垃圾行", lambda ls: [*ls, "这一行是人为写坏的垃圾内容"]),
            ("截断末行", lambda ls: ls[:-1]),
            ("末行改写为三字段行",
             lambda ls: [*ls[:-1], f"2026-08-29 00:00:31 {ls[-1].split()[-2]}"]),
        )
        for label, mutate in scenarios:
            with self.subTest(case=label):
                self._reset_state()
                lines = self._legacy_file()
                mutated = mutate(lines)
                self.assertNotEqual(mutated, lines, "前提：本场景真的改动了文件")
                self._write_lines(mutated)
                h = self._health()
                self.assertEqual(h["anchor_status"], "tampered", h["anchor_msg"])
                self.assertFalse(h["healthy"], h["anchor_msg"])


class AnchorPointRowNotExemptTest(_Fixture):
    """锚点定点行不享有清理留痕豁免：删它 + 种假留痕事件仍须判红。

    留痕住在应用可写的 app_meta 里；判据一若在 `anchored is None` 时先问"有没有一条
    事件恰好把它删掉了"，一条假事件就能把删尾翻成通过。锚点定点行按构造至多一个锚点
    间隔之旧，合法保留期清理只删月级窗口、够不到它，故取消豁免。
    """

    def _forge_purge_event(self, before_max, after_max):
        ev = {
            "kind": "audit_cleanup", "table": "audit_logs", "cutoff": "x",
            "deleted": 1, "before_min": 1, "before_max": before_max,
            "after_min": 1, "after_max": after_max,
            "ts": "2026-01-01 00:00:00", "audit_seq": 1,
        }
        self._raw("INSERT OR REPLACE INTO app_meta (key, value) VALUES (?,?)",
                  ("audit_purge_events", json.dumps([ev])))

    def test_forged_purge_event_cannot_excuse_anchor_point_row_deletion(self):
        self._seed(6)
        db.record_audit_anchor(self.anchor)
        self._raw("DELETE FROM audit_logs WHERE id = 6")      # 删锚点定点行
        self._forge_purge_event(before_max=6, after_max=5)    # 假事件声称覆盖 id=6
        h = self._health()
        self.assertFalse(h["healthy"], "留痕不得成为翻绿开关")
        self.assertEqual(h["anchor_status"], "tampered", h["anchor_msg"])
        self.assertIn("id=6", h["anchor_msg"])


class CliIndeterminateTest(_Fixture):
    """取证 CLI：无法定论必须 exit 2，不能用 exit 1 冒充"检出篡改"。"""

    @staticmethod
    def _out(r):
        enc = locale.getpreferredencoding(False) or "utf-8"
        try:
            return r.stdout.decode(enc)
        except (LookupError, UnicodeDecodeError):
            return r.stdout.decode("utf-8", errors="replace")

    def _run(self):
        env = dict(os.environ)
        env["YIBAN_DB_FILE"] = self.db_file
        env["YIBAN_ENV_FILE"] = self.env_file
        env["YIBAN_STATE_DIR"] = self.tmp
        return subprocess.run(
            [sys.executable, os.path.join(BASE, "scripts", "audit_verify.py"),
             "--anchor", self.anchor],
            capture_output=True, env=env, cwd=BASE,
        )

    def test_cli_exit_two_on_indeterminate_anchor(self):
        self._seed(3)
        db.record_audit_anchor(self.anchor)
        with open(self.anchor, "ab") as f:
            f.write(b"\xff\xfe\n")
        r = self._run()
        self.assertEqual(r.returncode, 2, self._out(r))
        self.assertIn("无法定论", self._out(r))


class CorruptAnchorMetaTest(_Fixture):
    """库内锚点指纹（audit_anchor_meta，应用可写）字段损坏不得让体检抛异常。

    该键住在 app_meta，值可被应用身份改写/手工损坏。`_anchor_file_state_ex` 读它的
    `lines` 字段时若直接 int()，一个非数字值就会抛 ValueError；调用点位于
    `_anchor_status` 的 try 之外，一次手工损坏即让每日体检整体抛异常、被 web 日线
    线程吞成 WARNING —— 当日校验静默不跑（正是被整改的失败形态）。加固：非数字值
    就地降级为 indeterminate/corrupt，healthy=False。
    """

    def test_corrupt_meta_lines_does_not_crash_health(self):
        self._seed(3)
        db.record_audit_anchor(self.anchor)
        self._raw("INSERT OR REPLACE INTO app_meta (key, value) VALUES (?,?)",
                  ("audit_anchor_meta",
                   json.dumps({"lines": "x", "last_hash": "", "ts": "x"})))
        h = self._health()  # 旧实现：audit_health 直接抛 ValueError
        self.assertEqual(h["anchor_status"], "indeterminate", h["anchor_msg"])
        self.assertFalse(h["healthy"], "损坏的库内指纹必须判不健康，不得静默")


class AlertDeliveryGatingTest(_Fixture):
    """告警基线只按**送达**推进：全通道失败不推进基线、下一轮重试。"""

    def test_send_notification_reports_delivery_from_any_channel(self):
        webapp = _load_webapp("alert_delivery_any")
        with mock.patch.object(webapp._notify_mail, "_alert_mail_recipients",
                               return_value=["a@test.local"]), \
             mock.patch.object(webapp, "_mail_alert_due", return_value=True), \
             mock.patch.object(webapp._notify_mail.mailer, "send_admin_alert",
                               return_value=True), \
             mock.patch.object(webapp._notify_mail.notify, "send", return_value=False):
            self.assertTrue(webapp.send_notification("t", "c"), "邮件送达即算送达")
        with mock.patch.object(webapp._notify_mail, "_alert_mail_recipients",
                               return_value=["a@test.local"]), \
             mock.patch.object(webapp, "_mail_alert_due", return_value=True), \
             mock.patch.object(webapp._notify_mail.mailer, "send_admin_alert",
                               return_value=False), \
             mock.patch.object(webapp._notify_mail.notify, "send", return_value=True):
            self.assertTrue(webapp.send_notification("t", "c"), "推送送达即算送达")
        with mock.patch.object(webapp._notify_mail, "_alert_mail_recipients",
                               return_value=["a@test.local"]), \
             mock.patch.object(webapp, "_mail_alert_due", return_value=True), \
             mock.patch.object(webapp._notify_mail.mailer, "send_admin_alert",
                               return_value=False), \
             mock.patch.object(webapp._notify_mail.notify, "send", return_value=False):
            self.assertFalse(webapp.send_notification("t", "c"), "两路皆失败必须返回 False")

    def test_unhealthy_alert_marks_baseline_only_when_delivered(self):
        webapp = _load_webapp("alert_gate_mark")
        self._seed(2)
        h = self._health()
        self.assertTrue(db.audit_alert_needs_attention(h), "夹具前提：首次结论待发")
        with mock.patch.object(webapp, "send_notification", return_value=True) as m_send:
            self.assertTrue(webapp._alert_audit_unhealthy(h))
        self.assertEqual(m_send.call_count, 1)
        self.assertFalse(db.audit_alert_needs_attention(h),
                         "送达后推进基线，同一故障态不再重发")

    def test_unhealthy_alert_retries_when_delivery_fails(self):
        webapp = _load_webapp("alert_gate_retry")
        self._seed(2)
        h = self._health()
        with mock.patch.object(webapp, "send_notification", return_value=False):
            self.assertFalse(webapp._alert_audit_unhealthy(h))
        self.assertTrue(db.audit_alert_needs_attention(h),
                        "全通道失败不得推进基线——下一轮必须仍待发")
        # 下一轮通道恢复：同一故障态重新外发并推进基线（未被上次失败永久静默）
        with mock.patch.object(webapp, "send_notification", return_value=True) as m_send:
            self.assertTrue(webapp._alert_audit_unhealthy(h))
        self.assertEqual(m_send.call_count, 1, "恢复送达后本次真正外发")
        self.assertFalse(db.audit_alert_needs_attention(h))


def _load_webapp(tag):
    """加载 web.app 做事实清单断言（与 test_audit_cleanup_visibility 同法）。"""
    spec = importlib.util.spec_from_file_location(
        f"webapp_{tag}", os.path.join(BASE, "web", "app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"webapp_{tag}"] = mod
    with contextlib.suppress(Exception):
        spec.loader.exec_module(mod)
    return mod


if __name__ == "__main__":
    unittest.main()
