# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""P8-E 判据：影子运行对账工具（scripts/shadow_compare.py，派单 2026-10-27）。

模拟的真实用户动作：运维在影子窗口让两栈各跑一轮后，把两侧当日终态
中间 JSONL 交给对账工具，读差异分类汇总。
断言的用户可见结果：同终态零差异；差异逐类归位（旧成功/新失败、窗口不同、
状态词不同、单侧无当日行、单侧无账号）；未知状态词不静默；输出零手机号明文。

夹具全合成值：手机号取 tests/synthetic-fixtures.json 登记表（1380000000X 段）。

词表形状考古（2026-10-27，ref 40504814）：
- 旧侧终态词＝yiban/core/status.py ALL_STATUSES；队列收尾形状见
  yiban/engine/executor_v3.py `_settle`（done 带 message、failed 带
  retry:/final: 前缀）与 yiban/adapters/persistence/migrations.py
  `_JSON_TERMINAL_TO_TASK_STATE`。
- 新侧词表＝server/internal/signing/contract/contract.go 的 Outcome 与
  server/internal/core/status/status.go 的 TaskState；结算分档见
  server/internal/app/worker/worker.go `settleOutcome`。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(BASE, "scripts", "shadow_compare.py")

DAY = "2026-01-01"
DAY2 = "2026-01-02"

# 登记过的合成手机号（tests/synthetic-fixtures.json）
PA = "13800000001"  # 两侧同终态
PB = "13800000002"  # 旧 already / 新 already_done（等价，应判 match）
PC = "13800000003"  # 旧 no_task / 新 skipped+no_target（等价，应判 match）
PD = "13800000004"  # 旧 paused / 新 failed+credential_invalid（等价，应判 match）
PE = "13800000005"  # 旧 failed / 新 failed+transient（同类，应判 match）
PF = "13800000006"  # 旧成功 / 新失败 → old_ok_new_failed
PG = "13800000007"  # 旧 skipped_window / 新成功 → window_mismatch
PH = "13800000008"  # 旧成功 / 新 pending+wait_window_open → window_mismatch
PI = "13800000009"  # 旧 failed / 新 skipped+no_target → status_word_diff


def _old(phone, status, **extra):
    row = {"phone": phone, "day": DAY, "status": status,
           "task_state": "", "task_result": "", "account_status": "active"}
    row.update(extra)
    return row


def _new(phone, state, result="", **extra):
    row = {"phone": phone, "day": DAY, "state": state, "result": result}
    row.update(extra)
    return row


_IDENTICAL_OLD = [
    _old(PA, "success"), _old(PB, "already"), _old(PC, "no_task"),
    _old(PD, "paused"), _old(PE, "failed"),
]
_IDENTICAL_NEW = [
    _new(PA, "success", "success"), _new(PB, "success", "already_done"),
    _new(PC, "skipped", "no_target"), _new(PD, "failed", "credential_invalid"),
    _new(PE, "failed", "transient"),
]


class ShadowCompareTest(unittest.TestCase):
    """判据：等价终态零差异、差异分类正确、未知词不静默、输出零明文手机号。"""

    def _run(self, old_rows, new_rows, extra=()):
        tmp = tempfile.TemporaryDirectory(prefix="yiban-p8e-")
        self.addCleanup(tmp.cleanup)
        old_path = os.path.join(tmp.name, "old.jsonl")
        new_path = os.path.join(tmp.name, "new.jsonl")
        out_path = os.path.join(tmp.name, "report.json")
        for path, rows in ((old_path, old_rows), (new_path, new_rows)):
            with open(path, "w", encoding="utf-8") as f:
                for row in rows:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
        r = subprocess.run(
            [sys.executable, SCRIPT, "--old", old_path, "--new", new_path,
             "--json", out_path, *extra],
            cwd=tmp.name, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60)
        return r, out_path

    def _report(self, r, out_path):
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(out_path, encoding="utf-8") as f:
            return json.load(f)

    def test_1_identical_terminal_states_zero_diff(self):
        """两侧同构终态 → 全 match，差异类全零。"""
        r, out = self._run(_IDENTICAL_OLD, _IDENTICAL_NEW)
        rep = self._report(r, out)
        self.assertEqual(rep["counts"]["match"], 5)
        for cat in ("old_ok_new_failed", "window_mismatch", "status_word_diff",
                    "one_side_no_row", "new_missing", "old_missing"):
            self.assertEqual(rep["counts"][cat], 0, f"{cat} 应为零")

    def test_2_diff_categories_land_separately(self):
        """五类差异各归各类；未定态计数不计差异。"""
        old_rows = [
            _old(PF, "success"),                       # 旧成功/新失败
            _old(PG, "skipped_window"),                # 窗口不同（旧窗口外跳过）
            _old(PH, "success"),                       # 窗口不同（新栈等窗口）
            _old(PI, "failed"),                        # 状态词不同
            _old("13800000002", "success"),            # 新栈无该账号
            _old(PG, "failed", day=DAY2),              # 单侧无当日行（旧多一天）
        ]
        new_rows = [
            _new(PF, "failed", "rejected"),
            _new(PG, "success", "success"),
            _new(PH, "pending", "wait_window_open"),
            _new(PI, "skipped", "no_target"),
            _new("13800000003", "success", "success"),  # 旧栈无该账号
        ]
        r, out = self._run(old_rows, new_rows)
        rep = self._report(r, out)
        want = {
            "old_ok_new_failed": 1,   # PF
            "window_mismatch": 2,     # PG + PH
            "status_word_diff": 1,    # PI
            "new_missing": 1,         # 13800000002
            "old_missing": 1,         # 13800000003
            "one_side_no_row": 1,     # PG 第二天
            "match": 0,
        }
        for cat, n in want.items():
            self.assertEqual(rep["counts"][cat], n, f"{cat} 计数不符")
        cats = {d["category"] for d in rep["diffs"]}
        self.assertNotIn("undecided", cats, "未定态不是差异")
        pg = [d for d in rep["diffs"]
              if d["phone"].endswith("0007") and d["day"] == DAY]
        self.assertEqual(pg[0]["category"], "window_mismatch")

    def test_3_undecided_not_counted_as_diff(self):
        """任一侧未定态 → 只进 undecided 计数，不进差异。"""
        r, out = self._run([_old(PA, "retrying")], [_new(PA, "success", "success")])
        rep = self._report(r, out)
        self.assertEqual(rep["counts"]["undecided"], 1)
        self.assertEqual(rep["counts"]["match"], 0)
        self.assertEqual(rep["diffs"], [])

    def test_4_unknown_word_flagged_then_mapping_resolves(self):
        """未登记旧词默认 unknown 入差异；--mapping 登记后判 match。"""
        old_rows = [_old(PA, "corp_paused")]
        new_rows = [_new(PA, "failed", "credential_invalid")]
        r, out = self._run(old_rows, new_rows)
        rep = self._report(r, out)
        self.assertEqual(rep["counts"]["status_word_diff"], 1)
        self.assertEqual(rep["diffs"][0]["old"]["class"], "unknown")
        mapping = os.path.join(os.path.dirname(out), "mapping.json")
        with open(mapping, "w", encoding="utf-8") as f:
            json.dump({"old": {"corp_paused": "cred_fail"}}, f)
        r2, out2 = self._run(old_rows, new_rows, extra=["--mapping", mapping])
        rep2 = self._report(r2, out2)
        self.assertEqual(rep2["counts"]["match"], 1)
        self.assertEqual(rep2["counts"]["status_word_diff"], 0)

    def test_5_bad_mapping_class_rejected(self):
        """mapping 里未知等价类 → 用法错误退出码 2。"""
        tmp = tempfile.TemporaryDirectory(prefix="yiban-p8e-badmap-")
        self.addCleanup(tmp.cleanup)
        mapping = os.path.join(tmp.name, "mapping.json")
        with open(mapping, "w", encoding="utf-8") as f:
            json.dump({"old": {"corp_paused": "not_a_class"}}, f)
        r = subprocess.run(
            [sys.executable, SCRIPT, "--old", os.path.join(tmp.name, "o.jsonl"),
             "--new", os.path.join(tmp.name, "n.jsonl"), "--mapping", mapping],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)

    def test_6_output_carries_no_full_phone(self):
        """stdout 与 JSON 报告零手机号明文，只出掩码形。"""
        old_rows = [_old(PF, "success"), _old("13800000002", "already")]
        new_rows = [_new(PF, "failed", "rejected")]
        r, out = self._run(old_rows, new_rows)
        rep = self._report(r, out)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for phone in (PF, "13800000002"):
            self.assertNotIn(phone, r.stdout, "stdout 泄漏手机号明文")
            self.assertNotIn(phone, json.dumps(rep, ensure_ascii=False),
                             "JSON 报告泄漏手机号明文")
        self.assertTrue(any(d["phone"].startswith("138****")
                            for d in rep["diffs"]), "明细应带掩码形")

    def test_7_missing_required_field_rejected(self):
        """行缺 phone/day 必填字段 → 退出码 2。"""
        r, _ = self._run([{"day": DAY, "status": "success"}], [])
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
