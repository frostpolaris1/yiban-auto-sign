# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""肘界影子分析脚本（`tools/elbow_shadow.py`）的守卫：判据不许恒真。

标签：B · 调度与领取
覆盖：
  ① **构造台账上判据可分辨**：给一份最小台账，含一条"提前量为正"（还没到期就被领）
     与一条"提前量足够负"（过期远超肘界）的领取记录，脚本必须分别判成"会拒/会领"，
     否则无法证明它在真数据上有效（恒真即废判据）。
  ② **与核心谓词同判**：脚本的判定规则逐例等于 `yiban.core.elbow.may_claim`
     （避免分析口径与线上口径漂移的第二份实现）。
  ③ **只读**：脚本只读打开库，绝不建库/写库；输出不出现原始手机号。
对应实现：`tools/elbow_shadow.py`（`classify` / `load_records` / `analyze` /
    `analyze_owners` / `main`）。
关键断言：① 是"判据不许恒真"的机械判据；② 是"两份口径不许漂移"的对拍。
"""
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(BASE, "tools", "elbow_shadow.py")

from tools import elbow_shadow  # noqa: E402
from yiban.core.elbow import ClaimRole, ClaimRow, may_claim  # noqa: E402

PHONE_A = "13800000001"
PHONE_B = "13900000002"


def _make_ledger(path):
    """造一份最小台账：一条提前量为正，一条过期远超肘界。"""
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE sign_tasks (phone TEXT, day TEXT, run_at TEXT, owner TEXT,"
        " state TEXT, PRIMARY KEY (phone, day));"
        "CREATE TABLE run_events (id INTEGER PRIMARY KEY, ts TEXT, day TEXT,"
        " node TEXT, executor TEXT, phone TEXT, message TEXT);"
    )
    # A：run_at 在领取时刻之后 600s ⇒ 提前量为正（还没到期就被领）。角色=worker。
    conn.execute("INSERT INTO sign_tasks VALUES (?,?,?,?,?)",
                 (PHONE_A, "2026-01-01", "2026-01-01 06:40:00.000", "worker-0@h", "pending"))
    conn.execute("INSERT INTO run_events (ts, day, node, executor, phone, message)"
                 " VALUES (?,?,?,?,?,?)",
                 ("2026-01-01 06:30:00", "2026-01-01", "claim", "worker-0@h", PHONE_A, ""))
    # B：run_at 比领取时刻早 1800s ⇒ 过期远超肘界（60s）。角色=兜底。
    conn.execute("INSERT INTO sign_tasks VALUES (?,?,?,?,?)",
                 (PHONE_B, "2026-01-01", "2026-01-01 06:30:00.000", "fallback@h", "pending"))
    conn.execute("INSERT INTO run_events (ts, day, node, executor, phone, message)"
                 " VALUES (?,?,?,?,?,?)",
                 ("2026-01-01 07:00:00", "2026-01-01", "claim", "fallback@h", PHONE_B, ""))
    conn.commit()
    conn.close()


class ClassifyTest(unittest.TestCase):
    """① 判据可分辨 + 边界。"""

    def test_positive_margin_is_rejected(self):
        self.assertEqual(elbow_shadow.classify(600.0, 60.0), "reject")

    def test_stale_margin_is_claimable(self):
        self.assertEqual(elbow_shadow.classify(-1800.0, 60.0), "claim")

    def test_exactly_at_elbow_is_claimable(self):
        self.assertEqual(elbow_shadow.classify(-60.0, 60.0), "claim")

    def test_just_inside_elbow_is_rejected(self):
        self.assertEqual(elbow_shadow.classify(-59.999, 60.0), "reject")


class CrossCheckWithCoreTest(unittest.TestCase):
    """② 脚本判据逐例等于核心谓词（口径不许漂移）。"""

    def test_classify_matches_core_predicate(self):
        cases = [600.0, 1.0, 0.0, -0.5, -30.0, -59.999, -60.0, -60.001, -1800.0]
        for elbow in (0.0, 30.0, 60.0, 120.0):
            for margin in cases:
                with self.subTest(elbow=elbow, margin=margin):
                    self.assertEqual(
                        elbow_shadow.classify(margin, elbow) == "claim",
                        may_claim(ClaimRow(run_at=margin), ClaimRole(elbow_sec=elbow),
                                  now=0.0),
                        f"脚本与核心谓词在 (margin={margin}, elbow={elbow}) 上判反")


class LedgerAnalysisTest(unittest.TestCase):
    """① 构造台账上读+判：两条记录必须被分开判。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="elbow-shadow-")
        self.db = os.path.join(self.tmp, "ledger.db")
        _make_ledger(self.db)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_records_are_read_with_margin(self):
        conn = elbow_shadow.open_readonly(self.db)
        try:
            records = elbow_shadow.load_records(conn)
        finally:
            conn.close()
        self.assertEqual(len(records), 2)
        roles = sorted(r["role"] for r in records)
        self.assertEqual(roles, ["fallback", "worker"])
        margins = sorted(r["margin_sec"] for r in records)
        self.assertAlmostEqual(margins[0], -1800.0, delta=1.0)
        self.assertAlmostEqual(margins[1], 600.0, delta=1.0)

    def test_analyze_separates_the_two_records(self):
        conn = elbow_shadow.open_readonly(self.db)
        try:
            stats = elbow_shadow.analyze(elbow_shadow.load_records(conn), 60.0)
        finally:
            conn.close()
        self.assertEqual(stats["claims"], 2)
        self.assertEqual(stats["would_reject"], 1,
                         "一条提前量为正必须被判成会拒；恒真判据会把它读成 0")

    def test_owner_absorption_reads_roles(self):
        conn = elbow_shadow.open_readonly(self.db)
        try:
            rows = elbow_shadow.analyze_owners(conn)
        finally:
            conn.close()
        by_role = {r["role"]: r["n"] for r in rows if r["day"] == "2026-01-01"}
        self.assertEqual(by_role, {"fallback": 1, "worker": 1})


class SubprocessE2ETest(unittest.TestCase):
    """③ 端到端：真跑脚本（子进程），只读、零手机号泄漏。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="elbow-shadow-e2e-")
        self.db = os.path.join(self.tmp, "ledger.db")
        _make_ledger(self.db)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cli_reports_the_rejection_and_masks_phones(self):
        out = subprocess.run(
            [sys.executable, SCRIPT, "--db", self.db, "--elbow-seconds", "60"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, f"脚本必须正常退出：\n{out.stdout}")
        self.assertIn("would_reject=1", out.stdout,
                      f"命令行输出必须报出被拒条数：\n{out.stdout}")
        for phone in (PHONE_A, PHONE_B):
            self.assertNotIn(phone, out.stdout, "输出不得出现原始手机号")


if __name__ == "__main__":
    unittest.main(verbosity=2)
