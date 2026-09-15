# -*- coding: utf-8 -*-
"""有效窗口单一口径（排计划 / 判关闭 / 算容量同源）。

**缺陷**：窗口被算了四遍且各不相同——排计划（`_schedule_blocks`，含"裁剪吃空则回退
默认窗口"）、判关闭（`_window_closed`，只看 sign_end−edge_back）、引擎容量预检
（内联算一遍，无回退也不扣流逝时间）、网页预估（自己读 env 再算一遍）。于是：

- 裁剪吃空时计划按回退窗口排（有 80 分钟计划），判定却按原始配置算
  → 有完整计划却整轮判"时段已结束"、零请求；容量还显示 0；
- 预检按完整窗口算，迟启动时按满容量放行且不告警，超出的账号落
  skipped_window；
- 补签轮起跑时窗口已关闭 → 整轮零请求，却把首轮已记录的
  failed/no_position 无条件改写成 skipped_window（真实原因与失败告警一起丢掉）。

**修法**：`yiban/window.py` 为唯一事实源，引擎与网页共用。
"""
import datetime as _dt
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import signin  # noqa: E402

from yiban import window  # noqa: E402

PHONE = "13800138000"


def _cfg(**env):
    """按给定 env 覆盖生成配置 dict（其余键取默认）。"""
    old = {k: os.environ.get(k) for k in env}
    try:
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = str(v)
        return signin._schedule_config()
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class WindowSourceTest(unittest.TestCase):
    def test_default_window(self):
        cfg = _cfg(YIBAN_SIGN_START=None, YIBAN_SIGN_END=None,
                   YIBAN_WINDOW_EDGE_FRONT_SEC=None, YIBAN_WINDOW_EDGE_BACK_SEC=None,
                   YIBAN_WINDOW_EDGE_SEC=None)
        win = window.bounds(cfg)
        self.assertEqual((win.start_min, win.end_min), (390, 470))
        self.assertEqual((win.lo_min, win.hi_min), (391.0, 469.0))
        self.assertFalse(win.fell_back)

    def test_legacy_edge_key_maps_symmetrically(self):
        cfg = _cfg(YIBAN_WINDOW_EDGE_SEC="120", YIBAN_WINDOW_EDGE_FRONT_SEC=None,
                   YIBAN_WINDOW_EDGE_BACK_SEC=None)
        self.assertEqual((cfg["edge_front_sec"], cfg["edge_back_sec"]), (120, 120))

    def test_sch3_trimmed_empty_window_falls_back_for_both_plan_and_gate(self):
        """裁剪吃空时，排计划与判关闭必须都按回退窗口（默认 06:30~07:50）。"""
        cfg = _cfg(YIBAN_SIGN_START="07:00", YIBAN_SIGN_END="07:01",
                   YIBAN_WINDOW_EDGE_FRONT_SEC="300", YIBAN_WINDOW_EDGE_BACK_SEC="300")
        win = window.bounds(cfg)
        self.assertTrue(win.fell_back, "前裁+后裁吃满窗口应回退默认窗口")
        self.assertEqual((win.lo_min, win.hi_min), (391.0, 469.0))
        # 计划（_schedule_blocks）与判定（_window_closed）同源：07:00 开启
        blocks, eff_lo, eff_hi = signin._schedule_blocks(cfg)
        self.assertGreater(len(blocks), 0, "回退窗口下应有完整计划")
        self.assertEqual((eff_lo, eff_hi), (win.lo_min, win.hi_min))
        self.assertFalse(
            signin._window_closed(cfg, _dt.datetime(2026, 9, 15, 7, 0)),
            "07:00 在回退窗口内，不得判『时段已结束』（原实现按原始配置算 → 全员零请求）",
        )
        self.assertTrue(signin._window_closed(cfg, _dt.datetime(2026, 9, 15, 9, 0)))

    def test_capacity_nonzero_train_on_trimmed_empty_window(self):
        """配置异常时容量不再显示 0（回退窗口仍有 80 分钟）。"""
        cfg = _cfg(YIBAN_SIGN_START="07:00", YIBAN_SIGN_END="07:01",
                   YIBAN_WINDOW_EDGE_FRONT_SEC="300", YIBAN_WINDOW_EDGE_BACK_SEC="300")
        win = window.bounds(cfg)
        self.assertGreater(signin.capacity_accounts(win.full_sec(), 10, 3), 0)

    def test_sch4_remaining_sec_deducts_elapsed(self):
        """剩余窗口 = 有效窗口结束 − 当前时刻（引擎预检口径）。"""
        cfg = _cfg(YIBAN_SIGN_START="06:30", YIBAN_SIGN_END="07:50",
                   YIBAN_WINDOW_EDGE_FRONT_SEC="60", YIBAN_WINDOW_EDGE_BACK_SEC="60")
        win = window.bounds(cfg)
        # eff_hi = 07:49；07:40 起跑 → 剩 9 分钟
        rest = win.remaining_sec(_dt.datetime(2026, 9, 15, 7, 40))
        self.assertAlmostEqual(rest, 9 * 60, delta=2)
        self.assertGreater(win.full_sec(), rest, "完整窗口必须大于剩余窗口")
        # 起跑已过窗口 → 剩余为负，调用方据此给出"本轮不会执行"的明确告警
        self.assertLess(win.remaining_sec(_dt.datetime(2026, 9, 15, 8, 10)), 0)

    def test_from_env_matches_cfg_path(self):
        """两条入口（env / cfg）必须得到同一结果，否则网页与引擎又会各算各的。"""
        env = {"YIBAN_SIGN_START": "06:40", "YIBAN_SIGN_END": "07:40",
               "YIBAN_WINDOW_EDGE_FRONT_SEC": "90", "YIBAN_WINDOW_EDGE_BACK_SEC": "30"}
        cfg = _cfg(**env)
        a, b = window.from_env(env), window.bounds(cfg)
        self.assertEqual((a.start_min, a.end_min, a.lo_min, a.hi_min),
                         (b.start_min, b.end_min, b.lo_min, b.hi_min))


class WindowSkipKeepsRecordedStatusTest(unittest.TestCase):
    """窗口关闭时的收尾不得覆盖已有当日记录。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-win-")
        self.env = dict(os.environ)
        os.environ["YIBAN_STATE_DIR"] = self.tmp
        os.environ["YIBAN_SIGN_START"] = "06:30"
        os.environ["YIBAN_SIGN_END"] = "07:50"
        os.environ.pop("YIBAN_SECOND_RUN", None)
        self.state_path = os.path.join(
            self.tmp, f"sign-state-{signin.clock.now().strftime('%Y-%m-%d')}.json")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_state(self, mapping):
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(mapping, f)

    def _run(self, phones, beijing_now):
        accs = [signin.Account(phone=p, password="p") for p in phones]
        with mock.patch.object(signin.clock, "now", return_value=beijing_now), \
                mock.patch.object(signin, "attempt_signin") as attempt, \
                mock.patch.object(signin, "_update_cred_state"), \
                mock.patch.object(signin.time, "sleep"):
            signin.run_queue_retry(accs, "", 0, 0,
                                  schedule={p: beijing_now for p in phones})
        return attempt

    def test_closed_window_keeps_previous_failure_reason(self):
        """已记录 failed 的账号：窗口关闭收尾不得改写成 skipped_window。"""
        self._write_state({PHONE: {"status": signin.STATUS_FAILED, "message": "网络超时"}})
        after = signin.clock.now().replace(hour=8, minute=5, second=0, microsecond=0)
        self._run([PHONE], after)
        with open(self.state_path, encoding="utf-8") as f:
            st = json.load(f)[PHONE]["status"]
        self.assertEqual(st, signin.STATUS_FAILED, "补签轮整轮跳过不得丢掉首轮失败原因")

    def test_closed_window_marks_unrecorded_accounts(self):
        """当日无记录的账号（本轮根本没跑到）：仍记为窗口外跳过。"""
        self._write_state({})
        after = signin.clock.now().replace(hour=8, minute=5, second=0, microsecond=0)
        self._run([PHONE], after)
        with open(self.state_path, encoding="utf-8") as f:
            st = json.load(f)[PHONE]["status"]
        self.assertEqual(st, signin.STATUS_SKIPPED_WINDOW)

    # 本轮内"重试没赶上窗口"与本轮前的记录共用同一道守卫（`results` 与当日文件
    # 都查），故上面两条覆盖了该规则的两种来源。

class SecondRunDropDoneTest(unittest.TestCase):
    """补签轮剔除已完成账号时，no_task 也算"已了结"。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-win2-")
        self.env = dict(os.environ)
        os.environ["YIBAN_STATE_DIR"] = self.tmp
        self.state_path = os.path.join(
            self.tmp, f"sign-state-{signin.clock.now().strftime('%Y-%m-%d')}.json")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_task_is_treated_as_done(self):
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump({PHONE: {"status": signin.STATUS_NO_TASK}}, f)
        kept = signin._second_run_drop_done([signin.Account(phone=PHONE, password="p")])
        self.assertEqual([a.phone for a in kept], [], "no_task 当日已了结，补签轮不应重登")

    def test_failed_is_kept_for_retry(self):
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump({PHONE: {"status": signin.STATUS_FAILED}}, f)
        kept = signin._second_run_drop_done([signin.Account(phone=PHONE, password="p")])
        self.assertEqual([a.phone for a in kept], [PHONE], "失败账号必须保留待补签")


if __name__ == "__main__":
    unittest.main(verbosity=2)
