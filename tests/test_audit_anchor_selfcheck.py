# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""审计锚点自检的活体反例：把输入改坏，工具必须响。

MF-52 的缺陷是"判据在自动层面等于没有"：锚点文件只判"行数变少/相等"两支，
**仅追加 1 条垃圾行**就让两道判据同时返回"无异常"；非法 UTF-8 抛
`UnicodeDecodeError`，被 web 每日线程的兜底 except 吞成一条 WARNING ⇒ 当日校验
整体不执行；无密钥即可伪造被采信的 v2 锚点行（`prev_line_hash` 是无密钥 sha256，
其余字段是库内明文副本）；兜住锚点的指纹存在库里、而锚点与库同为应用属主 ⇒
有双写权限者可抹掉最近 N 条审计且 `healthy=True`。

本文件按"每道自检必须自带一条把输入改坏 ⇒ 工具必须红"的活体反例纪律，逐条钉死
修复后的判据：行数三支（变少/相等/**变多**）、解析失败判"无法定论"（≠无异常也≠
普通红）、校验基准取 `max_id` 最大真行、以及跨权限独立见证对"双写掩盖"的检出。

标签：G · 安全：脱敏/审计/配置注入
覆盖：`_anchor_file_state_ex` 的行数三支与不可解析行、`_anchor_status` 的四态结论
（ok/none/tampered/indeterminate）、`record_audit_anchor_witness` 的单调写与三方
指纹一致性、`audit_health` 对"无法定论"的汇总与事实清单呈现、`record_audit_anchor`
对不可解析文件的拒绝续写、以及 `scripts/audit_verify.py` 对"无法定论"的 exit 2。
对应实现：`yiban/store/audit_chain.py`（`_anchor_status` / `_anchor_file_state_ex` /
`_anchor_witness_state` / `record_audit_anchor_witness` / `audit_health`）、
`web/services/notify_mail.py` 的 `_audit_alert_facts`、`scripts/audit_verify.py`。
关键断言：**"无法定论"必须与"无异常"和"确证篡改"两个结论都不同**——混进任一侧，
要么让一条非法字节把当日校验静默掉，要么把编码事故当失陷去响应；"变多"与"变少"
同等判红（正常写入每加一行都会抬高库内指纹高水位）；删掉独立见证后同一手双写会
重新通过，这正是已声明的降级残余风险，用它反证"拦住攻击的是独立见证本身"。
依赖：临时库 + 临时 `.env` + 临时锚点/见证文件；CLI 用例起真子进程，故依赖
`sys.executable` 并对子进程 stdout 按本地代码页解码；无网络、无 skip。
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
    """临时库 + 临时锚点 + 临时独立见证（三者互不干扰）。"""

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
        return db.audit_health(self.anchor, self.witness)

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
    """健康基线：真锚点 + 真库 + 真独立见证 ⇒ healthy=True 且见证跨权限标记到位。"""

    def test_clean_state_is_healthy_with_witness(self):
        self._seed(6)
        db.record_audit_anchor(self.anchor)
        state, msg = db.record_audit_anchor_witness(self.anchor, self.witness)
        self.assertEqual(state, "written", msg)
        h = self._health()
        self.assertEqual(h["anchor_status"], "ok", h["anchor_msg"])
        self.assertTrue(h["anchor_ok"])
        self.assertTrue(h["healthy"])
        # 同一属主的临时目录 → 降级标记；生产由 root 侧写入则应为 separate
        self.assertIn(h["anchor_witness"], ("same-owner", "separate", "unknown"))

    def test_witness_path_is_separate_from_anchor_path(self):
        """独立见证的**默认路径**必须与锚点分离：同目录 = 同权限 = 保护归零。"""
        anchor = db.audit_anchor_path()
        fp = db.audit_anchor_fingerprint_path()
        self.assertNotEqual(fp, anchor)
        self.assertNotEqual(os.path.basename(fp), os.path.basename(anchor))
        if os.name != "nt":
            # POSIX 生产形态：见证在 /var/lib/yiban-audit，锚点在状态目录（/var/log/yiban）
            self.assertNotEqual(os.path.dirname(fp), os.path.dirname(anchor))

    def test_witness_is_monotonic_and_idempotent(self):
        self._seed(3)
        db.record_audit_anchor(self.anchor)
        self.assertEqual(db.record_audit_anchor_witness(self.anchor, self.witness)[0], "written")
        with open(self.witness, encoding="utf-8") as f:
            first = f.read()
        self.assertEqual(db.record_audit_anchor_witness(self.anchor, self.witness)[0], "unchanged")
        with open(self.witness, encoding="utf-8") as f:
            self.assertEqual(f.read(), first, "无变化时不得重写见证")

    def test_witness_refuses_regression_without_overwrite(self):
        """锚点被截断后再跑见证：拒绝回退覆盖并留现场（覆盖掉等于替攻击者擦证据）。"""
        self._seed(4)
        db.record_audit_anchor(self.anchor)
        self._seed(1)
        db.record_audit_anchor(self.anchor)
        self.assertEqual(db.record_audit_anchor_witness(self.anchor, self.witness)[0], "written")
        with open(self.witness, encoding="utf-8") as f:
            before = f.read()
        self._write_lines(self._lines()[:1])  # 截断到最后一行之前
        state, _msg = db.record_audit_anchor_witness(self.anchor, self.witness)
        self.assertEqual(state, "regression")
        with open(self.witness, encoding="utf-8") as f:
            self.assertEqual(f.read(), before, "回退现场不得被覆盖")
        self.assertFalse(self._health()["healthy"], "截断必须判红")


class ThreeBranchJudgeTest(_Fixture):
    """行数判据三支齐全：变少 / 相等 / **变多** 都必须响。"""

    def test_garbage_line_append_goes_red_by_growth_branch(self):
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

    def test_valid_append_by_app_is_not_flagged(self):
        """正例对照：应用自己追加一行（库内高水位同步抬高）不得误报。"""
        self._seed(2)
        db.record_audit_anchor(self.anchor)
        # 生产形态：先落独立见证。否则"见证缺失"本身（控制面不可用）会判不健康，
        # 掩盖本用例真正要测的"合法追加不触发锚点三支判据"。
        self.assertEqual(db.record_audit_anchor_witness(self.anchor, self.witness)[0], "written")
        self._seed(2)
        db.record_audit_anchor(self.anchor)
        h = self._health()
        self.assertEqual(h["anchor_status"], "ok", h["anchor_msg"])
        self.assertTrue(h["healthy"], h["anchor_msg"])

    def test_truncation_still_goes_red(self):
        self._seed(3)
        db.record_audit_anchor(self.anchor)
        self._seed(3)
        db.record_audit_anchor(self.anchor)
        self._write_lines(self._lines()[:1])
        h = self._health()
        self.assertEqual(h["anchor_status"], "tampered")
        self.assertIn("减至", h["anchor_msg"])

    def test_claimed_count_mismatch_goes_red(self):
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


class IndeterminateStateTest(_Fixture):
    """解析失败 = "无法定论"：≠无异常、≠普通红，且 healthy=False。"""

    def test_invalid_utf8_is_indeterminate_not_healthy(self):
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
        ok, msg = db.verify_audit_anchor(self.anchor, self.witness)
        self.assertFalse(ok)
        self.assertTrue(msg.startswith("无法定论："), msg)

    def test_unparseable_line_without_meta_is_indeterminate(self):
        """无库内指纹可比时，结构坏行仍必须是"无法定论"而不是"无异常"。"""
        self._seed(2)
        with open(self.anchor, "w", encoding="utf-8") as f:
            f.write("完全是垃圾的一行\n")
        status, _msg = db._anchor_status(self.anchor, self.witness)
        self.assertEqual(status, "indeterminate")
        self.assertFalse(self._health()["healthy"])

    def test_alert_facts_name_indeterminate_and_witness(self):
        self._seed(4)
        db.record_audit_anchor(self.anchor)
        with open(self.anchor, "ab") as f:
            f.write(b"\xff\xfe\n")
        webapp = _load_webapp("anchor_selfcheck_facts")
        facts = dict(webapp._audit_alert_facts(self._health()))
        self.assertIn("无法定论", facts["库外锚点"])
        self.assertIn("锚点独立见证", facts, "降级/跨权限形态必须随告警出箱")
        self.assertIn("无法定论", facts["锚点说明"])

    def test_record_anchor_refuses_to_append_onto_broken_file(self):
        """读不出的锚点文件不得续写：按空列表续写会把行间链接到假前驱并掩盖现场。"""
        self._seed(3)
        db.record_audit_anchor(self.anchor)
        with open(self.anchor, "ab") as f:
            f.write(b"\xff\xfe\n")
        before = os.path.getsize(self.anchor)
        self.assertIsNone(db.record_audit_anchor(self.anchor))
        self.assertEqual(os.path.getsize(self.anchor), before, "拒绝续写必须是零写入")


class WitnessThreeWayTest(_Fixture):
    """三方指纹（库内 / 锚点旁路 / 独立文件）任一不一致 ⇒ 红。"""

    def _baseline_with_witness(self, n=10):
        self._seed(n)
        db.record_audit_anchor(self.anchor)
        self.assertEqual(db.record_audit_anchor_witness(self.anchor, self.witness)[0], "written")
        self.assertTrue(self._health()["healthy"], "夹具前提：见证后应健康")

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

    def test_witnessed_db_row_deleted_is_red(self):
        """见证记的链尾行从库里消失（只动库、不动锚点）⇒ 独立见证当场点出。"""
        self._baseline_with_witness(10)
        self._raw("DELETE FROM audit_logs WHERE id=10")
        h = self._health()
        self.assertEqual(h["anchor_status"], "tampered", h["anchor_msg"])
        self.assertIn("见证", h["anchor_msg"])
        self.assertIn("id=10", h["anchor_msg"])

    def test_padding_rewrite_after_witness_is_refused_and_red(self):
        """攻击变体①：见证已存在后把锚点**重写成更多行**（伪造自洽）⇒ 拒绝覆盖且判红。

        旧实现只在"行数相等"时比末行哈希；行数变多则不做任何内容校验地覆盖——root cron
        遂把伪造态 bless 成新见证，此后逐日校验对着假见证一路 healthy。前缀不变性是判据：
        见证记录的那一行必须还在原位且内容未变，合法追加（前缀完整）仍放行。
        """
        self._seed(6)
        db.record_audit_anchor(self.anchor)
        self.assertEqual(db.record_audit_anchor_witness(self.anchor, self.witness)[0], "written")
        # 攻击者：删掉最近 3 条审计，把锚点整份重写成两条"自洽"行（比见证的 1 行更多）
        self._raw("DELETE FROM audit_logs WHERE id > 3")
        l1 = f"2026-01-01 00:00:00 1 3 3 0 {self._row_hash(3)} {db._ANCHOR_GENESIS}"
        l2 = f"2026-01-01 00:00:01 1 3 3 0 {self._row_hash(3)} {db._anchor_line_sha(l1)}"
        self._write_lines([l1, l2])
        self._set_meta(2, db._anchor_line_sha(l2))
        # root cron 再跑一次见证：旧实现覆盖并 bless 伪造态，新实现必须拒绝
        state, _msg = db.record_audit_anchor_witness(self.anchor, self.witness)
        self.assertEqual(state, "rewritten",
                         "把锚点重写成更多行不得被见证覆盖（覆盖即 bless 伪造态）")
        self.assertFalse(self._health()["healthy"], "锚点历史被重写必须判红")

    def test_post_witness_tail_deletion_is_red(self):
        """攻击变体②：见证之后**新增**的审计被删（只动库）⇒ 体检判红。

        锚点与旧见证都只看锚点当时的状态，锚点之后 ~24h 内新增的审计行对两者都不可见，
        只删这些行即抹掉最近审计且 healthy=True。修复后见证回库记真实 max(id)+该行哈希，
        判据要求当前 max_id 不低于见证值且该行仍在。
        """
        self._seed(6)
        db.record_audit_anchor(self.anchor)
        self._seed(4)  # 锚点之后新增 4 条：旧实现锚点/见证都看不见
        self.assertEqual(db.record_audit_anchor_witness(self.anchor, self.witness)[0], "written")
        with open(self.witness, encoding="utf-8") as f:
            witness_json = json.load(f)
        self.assertEqual(witness_json.get("db_max_id"), 10,
                         "见证必须回库记下真实 max(id)——否则删尾无法检出")
        self._raw("DELETE FROM audit_logs WHERE id > 6")  # 只删见证之后新增的 4 条
        h = self._health()
        self.assertFalse(h["healthy"], "见证之后新增的审计被删必须判红")
        self.assertIn("见证", h["anchor_msg"])

    def test_corrupt_witness_json_is_indeterminate_not_crash(self):
        """手工损坏的见证 JSON（数值字段变成字符串）⇒ corrupt 降级，不得抛穿体检。

        见证文件是安装/运维侧可手工编辑的：字段非数字时旧实现的 `int()` 抛 ValueError，
        而 `_anchor_file_state_ex` 的调用位于 `_anchor_status` 的 try 之外——一次手工
        损坏就把每日体检印成一次崩溃，而不是"控制面损坏 ⇒ 不健康"。
        """
        self._baseline_with_witness(4)
        with open(self.witness, encoding="utf-8") as f:
            fp = json.load(f)
        fp["db_max_id"] = "not-a-number"
        with open(self.witness, "w", encoding="utf-8") as f:
            json.dump(fp, f)
        h = self._health()          # 旧实现：ValueError 穿出 audit_health
        self.assertFalse(h["healthy"])
        self.assertEqual(h["anchor_status"], "indeterminate", h["anchor_msg"])
        self.assertEqual(h["anchor_witness"], "corrupt")

    def test_double_write_attacker_is_caught_by_independent_file(self):
        """模拟双写攻击：改锚点 + 改库内指纹使两者自洽 ⇒ 独立文件不一致 ⇒ 红。

        这是 MF-52 唯一"权限实测"的缺口：锚点与库内指纹同属应用身份，删掉最近 N 条
        审计后把两者一起改写即可自洽。独立见证由另一属主写入，改不动，于是留下缺口。
        删掉见证后同一手双写不再有独立证据可比对（锚点判据确实看不出），但按控制面
        可用性 fail-closed，见证缺失本身即判不健康——攻击拿不到 healthy=True。
        """
        self._baseline_with_witness(10)
        with open(self.witness, encoding="utf-8") as f:
            legit_witness = f.read()          # 攻击前 root 侧写下的见证
        # 攻击者：删掉最近 5 条审计，再把锚点行与库内指纹一起改写成与新状态自洽
        self._raw("DELETE FROM audit_logs WHERE id > 5")
        forged = (f"2026-01-01 00:00:00 1 5 5 0 {self._row_hash(5)} "
                  f"{db._ANCHOR_GENESIS}")
        self._write_lines([forged])
        self._set_meta(1, db._anchor_line_sha(forged))
        # 降级形态（无独立见证）：锚点判据确实看不出这一手（anchor_ok=True），但
        # 见证控制面不可用（目录已预建、文件被删）⇒ fail-closed 判不健康，攻击拿不到绿。
        os.remove(self.witness)
        degraded = db.audit_health(self.anchor, self.witness)
        self.assertTrue(degraded["anchor_ok"],
                        "无独立见证时锚点三方判据确实看不出双写——这是降级形态的残余风险")
        self.assertFalse(degraded["healthy"],
                         "见证缺失（生产形态）即控制面不可用，必须判不健康")
        self.assertIn("独立见证", degraded["note"])
        # 把"攻击前"的见证放回去：同一手攻击立刻暴露
        with open(self.witness, "w", encoding="utf-8") as f:
            f.write(legit_witness)
        h = self._health()
        self.assertEqual(h["anchor_status"], "tampered", h["anchor_msg"])
        self.assertIn("见证", h["anchor_msg"])
        self.assertFalse(h["healthy"])


class PurgeEventForgeryTest(_Fixture):
    """可伪造的清理留痕不得解释掉被见证行：删尾 + 种一条假留痕事件仍须判红。

    `audit_purge_events` 与 `audit_purge_total` 都住在应用可写的 app_meta 里。旧判据在
    "被见证行已不存在 / 当前 max_id 低于见证值"两支上都先问 `_purge_event_covers`，于是
    删掉锚点之后新增的审计行、再种一条"把该 id 删掉了"的假事件（连累计数都不用动），
    体检即 green——留痕本身成了把篡改翻成通过的开关。

    被见证行按构造至多一个见证间隔（root 见证 cron 间隔）之旧，合法保留期清理只删
    月级窗口，永远够不到它；真要做整库/手工清理属 root 级维护，其运行手册步骤是重置
    独立见证文件（root 操作）再由下一轮 cron 重新播种——应用身份做不到。因此对**被
    见证行**取消留痕豁免：假事件只能解释更旧的锚点行，不能解释被见证行。
    """

    def _witness_after_post_anchor_rows(self, post=4):
        """真锚点 + 锚点之后新增 post 条 + 真见证（见证记下库内真实链尾 id）。"""
        self._seed(6)
        db.record_audit_anchor(self.anchor)
        self._seed(post)
        self.assertEqual(db.record_audit_anchor_witness(self.anchor, self.witness)[0], "written")
        with open(self.witness, encoding="utf-8") as f:
            w = json.load(f)
        self.assertEqual(w.get("db_max_id"), 6 + post,
                         "见证必须回库记下真实 max(id)——否则删尾无从检出")
        return w

    def _forge_purge_event(self, deleted, before_max, after_max, counter=None):
        """按 `_record_purge_event` 的真实字段形状种一条假留痕事件。

        形状与写库路径一致（表名/序号/前后 min-max），否则一眼可疑；攻击者只伪造
        "这次删除解释了被见证行"这一句。counter 非 None 时同步抬高累计数。
        """
        ev = {
            "kind": "audit_cleanup", "table": "audit_logs", "cutoff": "x",
            "deleted": deleted, "before_min": 1, "before_max": before_max,
            "after_min": 1, "after_max": after_max,
            "ts": "2026-01-01 00:00:00", "audit_seq": 1,
        }
        self._raw("INSERT OR REPLACE INTO app_meta (key, value) VALUES (?,?)",
                  ("audit_purge_events", json.dumps([ev])))
        if counter is not None:
            self._raw("INSERT OR REPLACE INTO app_meta (key, value) VALUES (?,?)",
                      ("audit_purge_total", str(counter)))

    def test_forged_purge_event_without_counter_cannot_excuse_deletion(self):
        """删掉锚点之后新增的审计 + 覆盖被见证 id 的假事件、不动累计数 ⇒ 仍判红。

        复现评审的活体反例：只动库（删 id>6）+ 种一条 before_max=10/after_max=6 的
        假事件，被见证行 id=10 在库内已不存在却被事件"解释"掉，旧实现在此返回
        healthy=True。
        """
        self._witness_after_post_anchor_rows()
        self._raw("DELETE FROM audit_logs WHERE id > 6")
        self._forge_purge_event(deleted=4, before_max=10, after_max=6)
        h = self._health()
        self.assertFalse(h["healthy"], "留痕住在应用可写的 app_meta，不得成为翻绿开关")
        self.assertEqual(h["anchor_status"], "tampered", h["anchor_msg"])
        self.assertIn("id=10", h["anchor_msg"], "必须点名被见证行")

    def test_forged_purge_event_with_counter_cannot_excuse_deletion(self):
        """连累计数一起配平、让假留痕与稠密判据自洽 ⇒ 被见证行仍不得被解释掉。

        留痕事件是攻击者自选的：把 deleted 与累计数设成恰好等于锚点那批消失的行数，
        稠密判据（missing == explained）就被喂饱；锚点行由事件 span 覆盖。旧实现下
        只剩被见证行也由同一条事件豁免——healthy=True。取消被见证行豁免后同样判红。
        """
        self._witness_after_post_anchor_rows()
        # 删掉锚点行及其后全部行：锚点那批恰好消失 4 条，配平累计数即自洽
        self._raw("DELETE FROM audit_logs WHERE id > 2")
        self._forge_purge_event(deleted=4, before_max=10, after_max=2, counter=4)
        h = self._health()
        self.assertFalse(h["healthy"], "累计数配平也解释不掉被见证行")
        self.assertEqual(h["anchor_status"], "tampered", h["anchor_msg"])
        self.assertIn("id=10", h["anchor_msg"])


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

    该键住在 app_meta，值可被应用身份改写/手工损坏。`_anchor_witness_state` 与
    `_anchor_file_state_ex` 读它的 `lines` 字段时若直接 int()，一个非数字值就会抛
    ValueError；两个调用点分别位于 `_anchor_status` 与 `audit_health` 的 try 之外，
    一次手工损坏即让每日体检整体抛异常、被 web 日线线程吞成 WARNING —— 当日校验
    静默不跑（正是 MF-52 的失败形态）。加固：库内字段与见证文件字段同等对待，
    损坏 ⇒ indeterminate/corrupt 降级，healthy=False。
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

    def test_corrupt_meta_lines_is_indeterminate_in_witness_state(self):
        """直接打 `_anchor_witness_state`：库内指纹行数非整数 ⇒ 无法定论（不抛）。"""
        line = f"2026-01-01 00:00:00 1 1 1 0 {'a' * 64} {'0' * 64}"
        with open(self.witness, "w", encoding="utf-8") as f:
            json.dump({"lines": 1, "line_hash": db._anchor_line_sha(line),
                       "max_id": 1, "head": "h"}, f)
        status = db._anchor_witness_state([line], {"lines": "x"}, self.witness, self.anchor)
        self.assertEqual(status[0], "indeterminate", status[1])


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
