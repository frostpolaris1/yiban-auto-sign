# -*- coding: utf-8 -*-
"""审计链自洽状态块（巡检面第二刀 ⑤，工单 yiban-auto-sign-vjy9）。

标签：E · Web：认证/权限/API
覆盖：`GET /api/admin/audit-status`（只读巡检端点）、`yiban.store.audit_chain` 的三态裁决
     `/ 数据库快照 / 已知欠账，以及 `scripts/audit_verify.py` 与端点的**同一份裁决**。

本文件钉**不变式**，不钉调用顺序：
  J1 三态互斥且只取三值：健康 / 检出篡改 / 无法定论；"判不出来"**不得**显示成健康。
  J2 端点的裁决码与 `scripts/audit_verify.py` 的退出码是**同一份**（同输入同码）。
  J3 已知欠账（锚点老格式行）单独成一条，且**不改变**三态（它不是篡改、也不是无法定论）。
  J4 端点**只读快照**，不触全表重算（`audit_health` 被打桩成抛异常时仍须 200）。
  J5 快照必须带数据时刻；无快照 ⇒ 无法定论 + 明说"尚无体检结果"。
  J6 只出计数与时刻，不出账号明细。

对应实现：yiban/adapters/persistence/audit_chain.py（audit_verdict / audit_anchor_summary /
     audit_known_debt / write_audit_health_snapshot / read_audit_health_snapshot）、
     web/routes/audit_api.py（api_audit_status）、web/app.py（每日线程写快照）。
"""
import contextlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))
sys.path.insert(0, BASE)


ADMIN_USER = "admin@test.local"
ADMIN_PASS = "TestPass1234!"
AUDIT_KEY = "b" * 64
VALID_STATES = {"healthy", "tampered", "indeterminate"}


def _load_webapp(name):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(BASE, "web", "app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    with contextlib.suppress(Exception):
        spec.loader.exec_module(mod)
    return mod


class _AuditFixtureBase(unittest.TestCase):
    """造一个真库 + 真锚点，供 CLI 与端点两条路读同一份事实。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-auditstatus-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write("YIBAN_ACCOUNTS_KEY=" + "a" * 64 + "\n")
            f.write(f"YIBAN_AUDIT_KEY={AUDIT_KEY}\n")
            f.write(f"YIBAN_ADMIN_USER={ADMIN_USER}\n")
            f.write(f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.anchor = os.path.join(cls.tmp, "audit-anchor.log")
        os.environ.update({
            "YIBAN_ENV_FILE": cls.env_file,
            "YIBAN_ACCOUNTS_KEY": "a" * 64,
            "YIBAN_AUDIT_KEY": AUDIT_KEY,
            "YIBAN_DB_FILE": cls.db_file,
            "YIBAN_STATE_DIR": cls.tmp,
            "YIBAN_LOG_FILE": os.path.join(cls.tmp, "sign.log"),
            "YIBAN_DISABLE_PURGE_LOOP": "1",
        })
        cls.webapp = _load_webapp("webapp_auditstatus")

    @classmethod
    def tearDownClass(cls):
        for key in ("YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_KEY", "YIBAN_AUDIT_KEY",
                    "YIBAN_DB_FILE", "YIBAN_STATE_DIR", "YIBAN_LOG_FILE",
                    "YIBAN_DISABLE_PURGE_LOOP"):
            os.environ.pop(key, None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        import db
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                os.remove(self.db_file + suffix)
        with contextlib.suppress(OSError):
            os.remove(self.anchor)
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)

    def _seed(self, n=6):
        import db
        for i in range(n):
            db.audit("tester", "seed", f"t{i}", f"d{i}")

    def _health(self):
        import db
        return db.audit_health(self.anchor)

    def _run_cli(self):
        """真跑取证 CLI，回它的退出码。"""
        proc = subprocess.run(
            [sys.executable, os.path.join(BASE, "scripts", "audit_verify.py"),
             "--db", self.db_file, "--env", self.env_file, "--anchor", self.anchor],
            capture_output=True, text=True)
        return proc.returncode

    def _login(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": ADMIN_USER, "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c

    def _status(self):
        r = self._login().get("/api/admin/audit-status")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()


class VerdictEqualsCliExitCodeTest(_AuditFixtureBase):
    """J1/J2：三态互斥，且端点裁决码 ≡ 取证 CLI 退出码（同一份裁决）。"""

    def _cases(self):
        """三种输入各建一次状态，产出 (标签, health, 期望码)。"""
        import db
        # ① 健康：真库 + 真锚点
        self._seed(6)
        db.record_audit_anchor(self.anchor)
        yield "healthy", self._health(), 0
        # ② 无法定论：锚点文件里只有老格式（三字段）行——没有 max_id 可定点
        self.setUp()
        self._seed(3)
        rows = [f"2026-08-21 00:00:31 {'c' * 64}"]
        with open(self.anchor, "w", encoding="utf-8") as f:
            f.write("\n".join(rows) + "\n")
        yield "indeterminate", self._health(), 2

    def test_verdict_matches_cli_exit_code(self):
        seen_states = set()
        for label, health, expected in self._cases():
            with self.subTest(case=label):
                import db
                verdict = db.audit_verdict(health)
                self.assertEqual(verdict["code"], expected,
                                 f"{label}: {verdict} health={health}")
                self.assertIn(verdict["state"], VALID_STATES)
                self.assertEqual(verdict["code"], self._run_cli(),
                                 f"{label}: 端点裁决码与 CLI 退出码分叉")
                seen_states.add(verdict["state"])
        self.assertEqual(seen_states, {"healthy", "indeterminate"})

    def test_tampered_case_is_code_one_and_not_healthy(self):
        import db
        self._seed(4)
        db.record_audit_anchor(self.anchor)
        with open(self.anchor, "a", encoding="utf-8") as f:
            f.write("这一行是人为写坏的垃圾内容\n")
        verdict = db.audit_verdict(self._health())
        self.assertEqual(verdict["state"], "tampered")
        self.assertEqual(verdict["code"], 1)
        self.assertEqual(verdict["code"], self._run_cli())

    def test_indeterminate_never_reports_healthy(self):
        """J1：'判不出来'不得折叠成健康。"""
        import db
        self.assertNotEqual(db.audit_verdict(
            {"chain_ok": True, "broken": 0, "anchor_ok": False,
             "anchor_status": "indeterminate", "anchor_msg": "坏行",
             "write_failures": 0, "empty_hash_rows": 0, "healthy": False})["state"],
            "healthy")
        # 锚点缺失（从未写过）= "未查"，同样不是健康
        self.assertEqual(db.audit_verdict(
            {"chain_ok": True, "broken": 0, "anchor_ok": True,
             "anchor_status": "none", "anchor_msg": "",
             "write_failures": 0, "empty_hash_rows": 0,
             "healthy": True})["state"], "indeterminate")


class KnownDebtTest(_AuditFixtureBase):
    """J3：老格式锚点行是**已知欠账**，单独成条，且不改变三态。"""

    def _write_legacy_prefix_anchor(self):
        """复刻生产形状：三字段前缀 + 正常行（jswn 之后的兼容口径判 ok）。"""
        import db
        self._seed(4)

        def row_hash(i):
            with db._conn_lock:
                r = db.get_conn().execute(
                    "SELECT hash FROM audit_logs WHERE id=?", (i,)).fetchone()
            return (r["hash"] or "") if r else None

        v0a = f"2026-08-21 00:00:31 {row_hash(1)}"
        v0b = f"2026-08-22 00:00:31 {row_hash(2)}"
        v1 = f"2026-08-28 00:00:31 1 3 {row_hash(3)}"
        v2 = "2026-09-01 00:00:31 1 4 4 0 {} {}".format(
            row_hash(4), db._anchor_line_sha(v1))
        with open(self.anchor, "w", encoding="utf-8") as f:
            f.write("\n".join([v0a, v0b, v1, v2]) + "\n")
        payload = json.dumps({"lines": 4, "last_hash": db._anchor_line_sha(v2), "ts": "x"})
        with db._conn_lock:
            conn = db.get_conn()
            conn.execute(
                "INSERT OR REPLACE INTO app_meta (key, value) "
                "VALUES ('audit_anchor_meta', ?)", (payload,))
            conn.commit()

    def test_old_format_lines_are_reported_as_known_debt_and_do_not_change_state(self):
        import db
        self._write_legacy_prefix_anchor()
        health = self._health()
        # 兼容已生效：老前缀本身不再把结论压成"无法定论"（jswn 已修）
        self.assertEqual(db.audit_verdict(health)["state"], "healthy")
        debt = db.audit_known_debt(self.anchor)
        item = [d for d in debt if d["code"] == "old_format_anchor_lines"]
        self.assertEqual(len(item), 1, debt)
        self.assertEqual(item[0]["count"], 2)
        # 欠账必须带原因，且**不得**被算进篡改/无法定论
        self.assertTrue(item[0]["reason"])
        self.assertNotIn(item[0]["count"], (0, None))

    def test_anchor_summary_counts_lines_and_定点(self):
        import db
        self._write_legacy_prefix_anchor()
        s = db.audit_anchor_summary(self.anchor)
        self.assertEqual(s["lines"], 4)
        self.assertEqual(s["old_format_lines"], 2)
        self.assertEqual(s["max_id"], 4)


class SnapshotReadOnlyEndpointTest(_AuditFixtureBase):
    """J4/J5/J6：端点读快照、标数据时刻、只出计数与时刻。"""

    def _write_snapshot(self, health, at=None):
        """写一份体检快照。`at=None` = 取当前业务钟（写方语义），**不得写死字面时刻**。

        本默认值一度是字面日期 `"2026-10-13 00:00:07"`。写入当天它新鲜；实现加入
        "快照超龄降级"（健康快照超 36 小时判 `indeterminate`）后，隔一天凡不显式传
        `at` 的用例集体转红（工单 5f9q）。夹具的"现在"必须是相对于 `yiban.clock`
        取出的当下，不能是常量——由 `tests/test_no_literal_date_in_fixtures.py` 钉住。
        """
        import db
        return db.write_audit_health_snapshot(health, path=self.anchor, at=at)

    def _corrupt_snapshot(self, mutate):
        """把已落库的快照 JSON 改坏（模拟"快照被篡改或损坏"——本端点自述的威胁模型）。"""
        import db
        snapshot = db.read_audit_health_snapshot()
        mutate(snapshot)
        with db._conn_lock:
            conn = db.get_conn()
            conn.execute(
                "INSERT OR REPLACE INTO app_meta (key, value) VALUES (?, ?)",
                ("audit_health_snapshot", json.dumps(snapshot, ensure_ascii=False)))
            conn.commit()

    def test_corrupt_snapshot_answers_200_and_indeterminate(self):
        """M1：快照损坏时端点**不得 500**——它正是用来显示"有没有被篡改"的那一面。

        损坏快照让它崩，既不假绿也不诚实回答。任何一种装配异常都必须落到
        "无法定论+尚无可用体检结果"，而不是把整页打成 5xx。
        """
        self._write_snapshot({"chain_ok": True, "broken": 0, "anchor_ok": True,
                              "anchor_status": "ok", "anchor_msg": "",
                              "write_failures": 0, "empty_hash_rows": 0,
                              "healthy": True})
        self._corrupt_snapshot(lambda s: s.__setitem__(
            "known_debt",
            [{"code": "old_format_anchor_lines", "label": "锚点老格式行",
              "count": "not-a-number", "reason": "坏值"}]))
        body = self._status()   # 当前实现在这里 500
        self.assertEqual(body["state"], "indeterminate")
        self.assertEqual(body["verdict_code"], 2)
        self.assertNotEqual(body["state"], "healthy", "损坏的快照不许报成健康")

    def test_other_corrupt_shapes_also_answer_200(self):
        """损坏形状的另外三支：`at` 非字符串、`health` 非 dict、`verdict` 非 dict。"""
        self._write_snapshot({"chain_ok": True, "broken": 0, "anchor_ok": True,
                              "anchor_status": "ok", "anchor_msg": "",
                              "write_failures": 0, "empty_hash_rows": 0,
                              "healthy": True})
        cases = (
            ("at 非字符串", lambda s: s.__setitem__("at", 12345)),
            ("health 非 dict", lambda s: s.__setitem__("health", ["坏"])),
            ("verdict 非 dict", lambda s: s.__setitem__("verdict", "坏")),
            ("known_debt 项非 dict", lambda s: s.__setitem__("known_debt", ["坏"])),
        )
        for label, mutate in cases:
            with self.subTest(case=label):
                self._write_snapshot({"chain_ok": True, "broken": 0, "anchor_ok": True,
                                      "anchor_status": "ok", "anchor_msg": "",
                                      "write_failures": 0, "empty_hash_rows": 0,
                                      "healthy": True})
                self._corrupt_snapshot(mutate)
                body = self._status()
                self.assertIn(body["state"], VALID_STATES, body)

    def test_stale_snapshot_downgrades_away_from_healthy(self):
        """M2：快照**超龄**即降级为"无法定论"——自检停摆时页面不许继续显示健康。

        这一条打的是整个第二刀 ⑤ 的核心承诺：绿色"健康"必须对应"最近真验过"。
        每日线程按业务日换日自检一次，故健康快照的正常年龄 ≤ 24 小时；40 小时前
        的快照只能说明"自检没在跑"。
        """
        from datetime import timedelta

        from yiban import clock
        old_at = (clock.now() - timedelta(hours=40)).strftime("%Y-%m-%d %H:%M:%S")
        self._write_snapshot({"chain_ok": True, "broken": 0, "anchor_ok": True,
                              "anchor_status": "ok", "anchor_msg": "",
                              "write_failures": 0, "empty_hash_rows": 0,
                              "healthy": True}, at=old_at)
        body = self._status()
        self.assertEqual(body["state"], "indeterminate", body)
        self.assertEqual(body["verdict_code"], 2)
        self.assertTrue(body["snapshot_stale"], body)
        self.assertTrue(body["indeterminate"]["present"])
        self.assertIn("超龄", body["indeterminate"]["reason"])

    def test_fresh_snapshot_is_not_downgraded(self):
        """边界另一头：刚刚写的快照不许被误降级（否则本块恒为黄，等于没修）。"""
        from datetime import timedelta

        from yiban import clock
        at = (clock.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        self._write_snapshot({"chain_ok": True, "broken": 0, "anchor_ok": True,
                              "anchor_status": "ok", "anchor_msg": "",
                              "write_failures": 0, "empty_hash_rows": 0,
                              "healthy": True}, at=at)
        body = self._status()
        self.assertEqual(body["state"], "healthy", body)
        self.assertFalse(body["snapshot_stale"], body)

    def test_tampered_state_is_not_downgraded_by_staleness(self):
        """超龄只降"健康"：篡改是确证结论，超龄不得把它洗成"无法定论"。"""
        from datetime import timedelta

        from yiban import clock
        at = (clock.now() - timedelta(hours=40)).strftime("%Y-%m-%d %H:%M:%S")
        self._write_snapshot({"chain_ok": False, "broken": 2, "anchor_ok": True,
                              "anchor_status": "ok", "anchor_msg": "",
                              "write_failures": 0, "empty_hash_rows": 1,
                              "healthy": False}, at=at)
        body = self._status()
        self.assertEqual(body["state"], "tampered", body)

    def test_endpoint_does_not_recompute_health(self):
        """J4：把全表体检打成抛异常，端点仍须 200（它只读快照）。"""
        health = {"chain_ok": True, "broken": 0, "anchor_ok": True,
                  "anchor_status": "ok", "anchor_msg": "", "write_failures": 0,
                  "empty_hash_rows": 0, "healthy": True}
        self._write_snapshot(health)
        from unittest import mock

        import db
        with mock.patch.object(db, "audit_health",
                               side_effect=AssertionError("巡检页不得触发全表重算")):
            body = self._status()
        self.assertEqual(body["state"], "healthy")
        self.assertTrue(body["cached"])

    def test_three_states_are_distinct_and_indeterminate_is_labeled(self):
        cases = (
            ({"chain_ok": True, "broken": 0, "anchor_ok": True,
              "anchor_status": "ok", "anchor_msg": "", "write_failures": 0,
              "empty_hash_rows": 0, "healthy": True}, "healthy", 0),
            ({"chain_ok": False, "broken": 2, "anchor_ok": True,
              "anchor_status": "ok", "anchor_msg": "", "write_failures": 0,
              "empty_hash_rows": 1, "healthy": False}, "tampered", 1),
            ({"chain_ok": True, "broken": 0, "anchor_ok": False,
              "anchor_status": "indeterminate", "anchor_msg": "锚点文件含非法字节",
              "write_failures": 0, "empty_hash_rows": 0, "healthy": False},
             "indeterminate", 2),
        )
        seen = set()
        for health, state, code in cases:
            with self.subTest(state=state):
                self._write_snapshot(health)
                body = self._status()
                self.assertEqual(body["state"], state, body)
                self.assertEqual(body["verdict_code"], code, body)
                self.assertIn(body["state"], VALID_STATES)
                seen.add(body["state"])
        self.assertEqual(seen, VALID_STATES, "三态必须都能被表示出来")

    def test_indeterminate_carries_reason_and_known_debt_is_separate(self):
        # 锚点文件里放两行老格式行 ⇒ 已知欠账非空，下面的字段检查才有内容可查
        with open(self.anchor, "w", encoding="utf-8") as f:
            f.write(f"2026-08-21 00:00:31 {'a' * 64}\n2026-08-22 00:00:31 {'b' * 64}\n")
        self._write_snapshot({"chain_ok": True, "broken": 0, "anchor_ok": False,
                              "anchor_status": "indeterminate", "anchor_msg": "锚点文件含非法字节",
                              "write_failures": 0, "empty_hash_rows": 0, "healthy": False})
        body = self._status()
        self.assertTrue(body["indeterminate"]["present"])
        self.assertTrue(body["indeterminate"]["reason"])
        self.assertTrue(body["known_debt"], "老格式行必须产出已知欠账条目")
        # 隐私：只出计数与时刻
        for key in ("phone", "account", "rows", "detail"):
            self.assertNotIn(key, body)
        # 已知欠账条目只带这四个键（多带一个即泄漏面的扩大，本断言逐条查键）
        for item in body["known_debt"]:
            self.assertEqual(set(item), {"code", "label", "count", "reason"}, item)
            self.assertEqual(item["count"], 2, item)
            self.assertNotIn("/", item["label"] + item["reason"], "不得回文件路径")

    def test_snapshot_carries_data_time(self):
        from datetime import timedelta

        from yiban import clock
        at = (clock.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        self._write_snapshot({"chain_ok": True, "broken": 0, "anchor_ok": True,
                              "anchor_status": "ok", "anchor_msg": "",
                              "write_failures": 0, "empty_hash_rows": 0,
                              "healthy": True}, at=at)
        body = self._status()
        self.assertEqual(body["data_at"], at)
        self.assertTrue(body["cached"])

    def test_snapshot_note_reaches_the_endpoint(self):
        """L1：`note` 必须随快照出到接口——它是 `audit_health` 的附加诊断文本。

        漏掉它会让端点顶层 `note` 恒为空串（审计链条的既有读方都把它显示给人看）。
        """
        self._write_snapshot({"chain_ok": True, "broken": 0, "anchor_ok": True,
                              "anchor_status": "ok", "anchor_msg": "",
                              "write_failures": 0, "empty_hash_rows": 0,
                              "note": "附加诊断：锚点自检提示", "healthy": True})
        body = self._status()
        self.assertEqual(body["note"], "附加诊断：锚点自检提示")

    def test_missing_snapshot_is_indeterminate_not_healthy(self):
        """J5：没有体检结果 = 无法定论（绝不当成健康）。"""
        import db
        with db._conn_lock:
            conn = db.get_conn()
            conn.execute("DELETE FROM app_meta WHERE key='audit_health_snapshot'")
            conn.commit()
        body = self._status()
        self.assertEqual(body["state"], "indeterminate")
        self.assertEqual(body["verdict_code"], 2)
        self.assertFalse(body["cached"])
        self.assertIsNone(body["data_at"])
        self.assertIn("尚无体检结果", body["reason"])

    def test_requires_login(self):
        c = self.webapp.create_app().test_client()
        self.assertEqual(c.get("/api/admin/audit-status").status_code, 401)


if __name__ == "__main__":
    unittest.main()
