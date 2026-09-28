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
`record_audit_anchor` 对不可解析文件的拒绝续写、以及 `scripts/audit_verify.py`
对"无法定论"的 exit 2。
对应实现：`yiban/store/audit_chain.py`（`_anchor_status` / `_anchor_file_state_ex` /
`audit_health`）、`web/services/notify_mail.py` 的 `_audit_alert_facts`、
`scripts/audit_verify.py`。
关键断言：**"无法定论"必须与"无异常"和"确证篡改"两个结论都不同**——混进任一侧，
要么让一条非法字节把当日校验静默掉，要么把编码事故当失陷去响应；"变多"与"变少"
同等判红（正常写入每加一行都会抬高库内指纹高水位）。
依赖：临时库 + 临时 `.env` + 临时锚点文件；CLI 用例起真子进程，故依赖
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
        ok, msg = db.verify_audit_anchor(self.anchor)
        self.assertFalse(ok)
        self.assertTrue(msg.startswith("无法定论："), msg)

    def test_unparseable_line_without_meta_is_indeterminate(self):
        """无库内指纹可比时，结构坏行仍必须是"无法定论"而不是"无异常"。"""
        self._seed(2)
        with open(self.anchor, "w", encoding="utf-8") as f:
            f.write("完全是垃圾的一行\n")
        status, _msg = db._anchor_status(self.anchor)
        self.assertEqual(status, "indeterminate")
        self.assertFalse(self._health()["healthy"])

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
