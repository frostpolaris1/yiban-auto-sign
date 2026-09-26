# -*- coding: utf-8 -*-
"""窗口告警去重标记的业务日复位点（三个标记逐一）。

标签：J · 运维：部署/备份/发布
覆盖：`yiban/engine/schedule.py` 三个一次性告警标记（窗口配置非法 / 缓冲被收缩 / 窗口回退）在业务日翻页后复位，因而第二天能再次触发并再次并入汇总邮件；同一业务日内的多轮调用仍然只并入一次
对应实现：`yiban/engine/schedule.py` 的 `reset_daily_alerts`（唯一复位点）与 `_schedule_config` / `_schedule_blocks` 里三个标记的置位分支
关键断言：翻页前一轮只发一次、翻页后当轮**再次**发一次（当前红：标记全仓无生产复位点，常驻兜底进程不重启就永远无声）；同一业务日重复调用不得多发（复位不能退化成本轮次重置）
依赖：纯本地——mock `yiban.engine.schedule.clock.now` 造业务日翻页 + mock 告警出口计数；临时环境变量在类边界还原；不起子进程、不联网、不需 bash/docker

**为什么需要**：这三个标记是"同一配置错误只并入当日汇总邮件一次"的去重位，但它们原先
只在模块导入时为假，**全仓没有任何生产复位点**。引擎的兜底常驻进程（每 60 秒扫一轮）
不会重启，于是同一配置错误在第一天报过之后，第二天起彻底无声——管理员此后看到的是
"一切正常"，而配置依旧是错的。复位点必须显式存在：这里取**业务日翻页**（翻页必然早于
当日窗口开启，等价于"新窗口开启时复位"，但只有一个时点可判）。
"""
import os
import unittest
import unittest.mock as mock
from datetime import datetime

from yiban.engine import schedule

DAY1 = datetime(2026, 9, 23, 6, 40)     # 周三
DAY2 = datetime(2026, 9, 24, 6, 40)     # 周四
DAY3 = datetime(2026, 9, 25, 6, 40)     # 周五

VALID_WINDOW = {"YIBAN_SIGN_START": "06:30", "YIBAN_SIGN_END": "07:50"}
INVALID_WINDOW = {"YIBAN_SIGN_START": "07:00", "YIBAN_SIGN_END": "06:00"}


def _overflow_cfg():
    """06:30~06:40 共 10 分钟窗口、前后各裁 300s ⇒ 缓冲合计吃满窗口（clamped 分支）。"""
    return {"sign_start": (6, 30), "sign_end": (6, 40),
            "edge_front_sec": 300, "edge_back_sec": 300}


def _degenerate_cfg():
    """窗口宽度 <= 0 ⇒ fell_back 分支（回退默认窗口）。"""
    return {"sign_start": (7, 0), "sign_end": (6, 0),
            "edge_front_sec": 300, "edge_back_sec": 300}


class _ResetBase(unittest.TestCase):
    def setUp(self):
        self._env_backup = {
            k: os.environ.get(k) for k in
            ("YIBAN_SIGN_START", "YIBAN_SIGN_END", "YIBAN_WINDOW_EDGE_SEC",
             "YIBAN_WINDOW_EDGE_FRONT_SEC", "YIBAN_WINDOW_EDGE_BACK_SEC")
        }
        for k, v in VALID_WINDOW.items():
            os.environ[k] = v
        for k in ("YIBAN_WINDOW_EDGE_SEC", "YIBAN_WINDOW_EDGE_FRONT_SEC",
                  "YIBAN_WINDOW_EDGE_BACK_SEC"):
            os.environ.pop(k, None)
        # 白盒复位：把三个标记与"已复位到哪个业务日"一起清回初始态（测试间互不影响）
        schedule._alert_mark_day = ""
        schedule._invalid_window_notified = False
        schedule._window_clamped_notified = False
        schedule._window_fallback_notified = False

    def tearDown(self):
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class WindowAlertResetTest(_ResetBase):
    """三个标记逐一：翻页可复位、同日不重复。"""

    def test_invalid_window_alert_fires_again_on_the_next_business_day(self):
        """窗口配置非法：翻页前一次、翻页后**再**一次（当前红：永久无声）。"""
        os.environ.update(INVALID_WINDOW)
        with mock.patch("yiban.engine.alerts._collect_admin_mail") as m_mail, \
                mock.patch.object(schedule.clock, "now", return_value=DAY1):
            schedule._schedule_config()
            schedule._schedule_config()      # 同一业务日的多轮：只并入一次
            self.assertEqual(m_mail.call_count, 1)
        with mock.patch("yiban.engine.alerts._collect_admin_mail") as m_mail, \
                mock.patch.object(schedule.clock, "now", return_value=DAY2):
            schedule._schedule_config()
            self.assertEqual(m_mail.call_count, 1,
                             "翻页后同一配置错误必须再发一次（否则常驻进程第 2 天起无声）")

    def test_clamped_buffer_alert_fires_again_on_the_next_business_day(self):
        """缓冲被收缩：同一业务日一次、第二天再一次。"""
        with mock.patch("yiban.engine.alerts._collect_admin_mail") as m_mail, \
                mock.patch.object(schedule.clock, "now", return_value=DAY1):
            schedule._schedule_config()
            schedule._schedule_blocks(_overflow_cfg())
            schedule._schedule_blocks(_overflow_cfg())
            self.assertEqual(m_mail.call_count, 1)
        with mock.patch("yiban.engine.alerts._collect_admin_mail") as m_mail, \
                mock.patch.object(schedule.clock, "now", return_value=DAY2):
            schedule._schedule_config()
            schedule._schedule_blocks(_overflow_cfg())
            self.assertEqual(m_mail.call_count, 1, "翻页后缓冲收缩告警必须再发一次")

    def test_fallback_alert_fires_again_on_the_next_business_day(self):
        """窗口不可用被回退：同一业务日一次、第二天再一次。"""
        with mock.patch("yiban.engine.alerts._collect_admin_mail") as m_mail, \
                mock.patch.object(schedule.clock, "now", return_value=DAY1):
            schedule._schedule_config()
            schedule._schedule_blocks(_degenerate_cfg())
            self.assertEqual(m_mail.call_count, 1)
        with mock.patch("yiban.engine.alerts._collect_admin_mail") as m_mail, \
                mock.patch.object(schedule.clock, "now", return_value=DAY2):
            schedule._schedule_config()
            schedule._schedule_blocks(_degenerate_cfg())
            self.assertEqual(m_mail.call_count, 1, "翻页后回退告警必须再发一次")


class ResetPointTest(_ResetBase):
    """复位点的语义：业务日粒度、一次把三个标记都清掉、入口自带（无需调用方记得调）。"""

    def test_reset_is_per_business_day_not_per_call(self):
        """同一业务日重复调用不再复位——否则退化成"每轮一次"，去重失效、邮件刷屏。"""
        self.assertTrue(schedule.reset_daily_alerts(DAY1))
        schedule._invalid_window_notified = True
        schedule._window_clamped_notified = True
        schedule._window_fallback_notified = True
        self.assertFalse(schedule.reset_daily_alerts(DAY1), "同一业务日不得二次复位")
        self.assertTrue(schedule._invalid_window_notified)
        self.assertTrue(schedule._window_clamped_notified)
        self.assertTrue(schedule._window_fallback_notified)

    def test_reset_clears_all_three_markers_on_a_new_day(self):
        schedule.reset_daily_alerts(DAY1)
        schedule._invalid_window_notified = True
        schedule._window_clamped_notified = True
        schedule._window_fallback_notified = True
        self.assertTrue(schedule.reset_daily_alerts(DAY2))
        self.assertFalse(schedule._invalid_window_notified)
        self.assertFalse(schedule._window_clamped_notified)
        self.assertFalse(schedule._window_fallback_notified)
        schedule.reset_daily_alerts(DAY3)
        self.assertEqual(schedule._alert_mark_day, DAY3.strftime("%Y-%m-%d"))

    def test_schedule_config_calls_the_reset_point(self):
        """复位点必须挂在每日必过的调度入口上（调用方不必记得手动调）。"""
        with mock.patch.object(schedule, "reset_daily_alerts", return_value=False) as m_reset, \
                mock.patch.object(schedule.clock, "now", return_value=DAY1):
            schedule._schedule_config()
        m_reset.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
