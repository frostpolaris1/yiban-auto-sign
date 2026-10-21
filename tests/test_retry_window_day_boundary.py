# -*- coding: utf-8 -*-
"""M16：重试落点按**绝对时刻**夹进同一时间窗，不拿「当天第几分钟」配零点。

标签：B · 调度：窗口/重试
覆盖：`next_retry_at_v3` 的落点夹取在「已进窗口 / 未进窗口 / 窗口已过」三段的行为，
   以及 `Window.bounds_dt` 把分钟数配成绝对时刻这件事本身。

对应实现：yiban/core/window.py（`Window.bounds_dt`）、yiban/engine/executor_v3.py
   （`next_retry_at_v3`）。

关键断言：**跨零点后重试不得落在窗口之外**。`lo_min` / `hi_min` 是「当天第几分钟」、
   不含日期；长轮次跨过午夜后，`remaining_sec` 把 00:10 算成"今天还剩一整窗"，
   而用「零点 + hi_min」当上界算的是**次日**的窗口结束——`now + delay` 于是小于上界
   而原样胜出，落点变成 00:1x。通道只判"窗口没关"（对 00:10 为假），那条行遂在
   窗口外被真实登录（还会被记成 `skipped_window`）。判据是"落点必须落在有效窗口内"，
   不是"能算出个时间"。

依赖：纯函数 + 固定 cfg 快照；不碰数据库、不发网络请求、不起应用。整文件在本机执行，
   无 skip。
"""
import datetime
import os
import random
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from yiban.core import window
from yiban.engine import executor_v3, schedule


def _cfg(**over):
    """`schedule.planner_config()` 的同形配置快照（不读真实 .env）。"""
    cfg = {
        "edge_front_sec": 60,
        "edge_back_sec": 60,
        "avg_attempt_sec": 3,
        "retry_min_interval": 60,
        "sign_start": (6, 30),
        "sign_end": (7, 50),
    }
    cfg.update(over)
    return cfg


#: 默认窗口 06:30~07:50、前后各让 60s ⇒ 有效窗口 06:31:00 ~ 07:49:00
DAY1 = datetime.datetime(2026, 9, 22)
DAY2 = datetime.datetime(2026, 9, 23)   # 跨零点后的那一天
LO = datetime.time(6, 31, 0)
HI = datetime.time(7, 49, 0)


class BoundsDtTest(unittest.TestCase):
    """`Window.bounds_dt`：分钟数 + 日期 → 绝对时刻。"""

    def test_returns_absolute_edges_of_the_given_day(self):
        win = window.bounds(_cfg())
        lo_dt, hi_dt = win.bounds_dt(DAY1.replace(hour=23, minute=59))
        self.assertEqual(lo_dt, datetime.datetime.combine(DAY1.date(), LO))
        self.assertEqual(hi_dt, datetime.datetime.combine(DAY1.date(), HI))

    def test_same_minutes_on_different_days_are_different_instants(self):
        """同一组分钟数配不同日期 ⇒ 不同的绝对时刻。

        这正是 M16 的根：`hi_min` 不含日期，跨零点后"次日的 07:49"必须与"今日的
        07:49"是两个时刻，不能互相冒充。
        """
        win = window.bounds(_cfg())
        self.assertNotEqual(win.bounds_dt(DAY1)[1], win.bounds_dt(DAY2)[1])

    def test_accepts_plain_date(self):
        win = window.bounds(_cfg())
        self.assertEqual(win.bounds_dt(DAY1.date())[0],
                         datetime.datetime.combine(DAY1.date(), LO))


class RetryLandingTest(unittest.TestCase):
    """落点三段：已进窗口 / 未进窗口 / 窗口已过。"""

    def _in_window(self, target, day):
        lo_dt, hi_dt = window.bounds(_cfg()).bounds_dt(day)
        self.assertIsNotNone(target, "落点必须存在（窗口未过）")
        self.assertGreaterEqual(target, lo_dt,
                                f"落点 {target} 早于有效窗口起点 {lo_dt} ⇒ 窗口外登录")
        self.assertLessEqual(target, hi_dt,
                             f"落点 {target} 晚于有效窗口终点 {hi_dt} ⇒ 窗口外登录")

    # ---- 正常路径：行为必须逐字不变 ----
    def test_inside_window_lands_within_window(self):
        now = DAY1.replace(hour=6, minute=40)
        for last_delay in (60, 600, 10 ** 6):
            with self.subTest(last_delay=last_delay):
                self._in_window(
                    executor_v3.next_retry_at_v3(now, _cfg(), last_delay,
                                                 random.Random(5)), DAY1)

    def test_inside_window_keeps_legacy_delay_arithmetic(self):
        """窗口内的落点 = `min(now + delay, hi)`，与旧实现同值（不夹到窗口起点）。

        已进窗口时把落点往上顶到 `lo_dt` 会**推迟**本该立刻发生的重试——那是行为回归。
        """
        now = DAY1.replace(hour=6, minute=40)
        lo_dt, hi_dt = window.bounds(_cfg()).bounds_dt(DAY1)
        for seed in range(1, 12):
            with self.subTest(seed=seed):
                target = executor_v3.next_retry_at_v3(now, _cfg(), 60, random.Random(seed))
                lo_legacy, hi_legacy = 60.0, executor_v3.RETRY_CAP_SEC
                self.assertGreater(target, lo_dt)
                self.assertLessEqual((target - now).total_seconds(), hi_legacy)
                self.assertLessEqual(target, hi_dt)
                self.assertGreaterEqual((target - now).total_seconds(), lo_legacy)

    # ---- M16 现场：跨零点 ----
    def test_after_midnight_lands_in_same_window_not_before_it(self):
        """跨零点的长轮次：00:10 的重试必须落在**次日同一时间窗**内，而不是 00:1x。

        默认窗口 06:30~07:50。旧口径下 `remaining_sec(00:10)` 认为"还剩一整窗"，
        落点被 `now + delay` 支配 → 00:1x，窗口**之前**；通道只判"窗口没关"，
        那条行遂在窗口外被真实登录。
        """
        now = DAY2.replace(hour=0, minute=10)
        for last_delay in (60, 600, 10 ** 6):
            for seed in range(1, 8):
                with self.subTest(last_delay=last_delay, seed=seed):
                    target = executor_v3.next_retry_at_v3(
                        now, _cfg(), last_delay, random.Random(seed))
                    self._in_window(target, DAY2)
                    self.assertGreaterEqual(target, DAY2.replace(hour=6, minute=31),
                                            "跨零点后的重试必须落在次日窗口起点之后")

    def test_after_midnight_keeps_backoff_spacing_inside_the_window(self):
        """钉进窗口不等于吞掉退避：落点仍应体现 delay（越往后越靠后）。

        直接把落点钉死在 `lo_dt` 会让所有重试挤在窗口第一分钟（尾端扎堆的反面）。
        """
        now = DAY2.replace(hour=0, minute=10)
        lo_dt, _hi = window.bounds(_cfg()).bounds_dt(DAY2)
        early = executor_v3.next_retry_at_v3(now, _cfg(), 60, random.Random(1))
        late = executor_v3.next_retry_at_v3(now, _cfg(), 600, random.Random(1))
        self.assertGreaterEqual(early, lo_dt)
        self.assertGreaterEqual(late, lo_dt)
        self.assertGreater(late, early, "更大的 last_delay 必须把落点往后推（退避语义）")

    def test_before_window_opens_same_day_also_lands_inside(self):
        """提前拉起的执行体（06:00 起跑）同样不得把落点甩在窗口之前。

        与跨零点同一根因：`now + delay` 小于窗口起点时没有任何东西把它夹回来。
        """
        now = DAY1.replace(hour=6, minute=0)
        for seed in range(1, 8):
            with self.subTest(seed=seed):
                self._in_window(
                    executor_v3.next_retry_at_v3(now, _cfg(), 60, random.Random(seed)),
                    DAY1)

    # ---- 窗口已过：行为不变 ----
    def test_window_spent_returns_none(self):
        now = DAY1.replace(hour=7, minute=49, second=1)
        self.assertIsNone(executor_v3.next_retry_at_v3(now, _cfg(), 60))

    def test_still_inside_window_at_the_last_second_is_not_none(self):
        """窗口只剩 1s：`cap` 已小于最小间隔 → 放弃重试（None），不把间隔压到 60s 之下。

        这是 ba-p03-04 的不变量：`YIBAN_RETRY_MIN_INTERVAL` 是硬下界。降到 1s 下界时，
        1s 的剩余窗口仍容不下（cap=0.3 < 1）；窗口剩 5s 时 cap=1.5 ≥ 1，应给出窗内落点。
        """
        last_second = DAY1.replace(hour=7, minute=48, second=59)
        self.assertIsNone(executor_v3.next_retry_at_v3(
            last_second, _cfg(), 60, random.Random(2)))
        cfg_low = _cfg(retry_min_interval=1)
        self.assertIsNone(executor_v3.next_retry_at_v3(
            last_second, cfg_low, 60, random.Random(2)),
            "1s 剩余窗口连 1s 下界也放不下 ⇒ 仍须 None")
        now = DAY1.replace(hour=7, minute=48, second=55)
        target = executor_v3.next_retry_at_v3(now, cfg_low, 60, random.Random(2))
        self.assertIsNotNone(target)
        self._in_window(target, DAY1)

    # ---- 退化窗口：夹取不得越界 ----
    def test_narrow_window_still_clamps_inside(self):
        """窗口极窄时（只剩 10s）：默认最小间隔放不下 ⇒ None；降下界后落点仍在窗内。"""
        cfg = _cfg(sign_end=(6, 42), edge_back_sec=110, edge_front_sec=0)
        now = DAY1.replace(hour=6, minute=40)
        _lo, hi_dt = window.bounds(cfg).bounds_dt(DAY1)
        for last_delay in (60, 600, 10 ** 6):
            with self.subTest(last_delay=last_delay):
                self.assertIsNone(executor_v3.next_retry_at_v3(
                    now, cfg, last_delay, random.Random(5)))
        cfg_low = dict(cfg, retry_min_interval=1)
        for last_delay in (60, 600, 10 ** 6):
            with self.subTest(last_delay=last_delay, retry_min_interval=1):
                target = executor_v3.next_retry_at_v3(
                    now, cfg_low, last_delay, random.Random(5))
                self.assertIsNotNone(target)
                self.assertLessEqual(target, hi_dt)

    def test_business_day_passed_after_midnight_returns_none(self):
        """业务日仍是昨天时，跨过午夜后的重试落点必须是 None（ba-p03-06）。

        `day` 是队列行所属的业务日；`bounds_dt(业务日)` 的上界在昨天，00:10 时剩余为负。
        旧实现只喂墙钟时刻，会把落点算到"次日窗口"而把行留在昨日的 `day` 上——按日
        领取的队列永远领不到它（静默漏签）。故这里钉的是"绝不返回一个属于次日的落点"。
        """
        now = DAY2.replace(hour=0, minute=10)
        self.assertIsNone(executor_v3.next_retry_at_v3(
            now, _cfg(), 60, random.Random(1), day=DAY1.strftime("%Y-%m-%d")))

    def test_business_day_inside_window_lands_on_that_day(self):
        """正常路径：显式业务日与 now 同日时，落点仍落在该业务日的窗口内。"""
        now = DAY1.replace(hour=6, minute=40)
        day = DAY1.strftime("%Y-%m-%d")
        target = executor_v3.next_retry_at_v3(now, _cfg(), 60, random.Random(5), day=day)
        self.assertIsNotNone(target)
        self.assertEqual(target.date(), DAY1.date())
        self._in_window(target, DAY1)


class RetryBudgetInvariantTest(unittest.TestCase):
    """ksn1：退避预算 ≤ 尾部预留缓冲——把「窗口留得太短」暴露在设计期。

    `next_retry_at_v3` 的落点上界是 `min(RETRY_CAP_SEC, 剩余有效窗口 × 0.3)`。
    「尾部预留缓冲」= 这条式子能从窗口尾部拿到的最大额度（窗口起点处的
    `有效窗口 × 0.3`）。若它小于 `RETRY_CAP_SEC`，配置的退避上限**永远拿不到**：
    函数每一拍都会静默把 cap 压低，日志里没有任何一句话说"你配的 600s 不可达"。
    这条不变量把该形态变成显式报错（`schedule._schedule_config` 记录 ERROR）。

    钉的是**配置组合上的性质**，不是某次调用的返回值：窗口宽度足以容纳硬上限时
    必须无违规；窗口一旦短到尾部预算低于硬上限，必须报违规。
    """

    def test_default_window_affords_the_full_retry_cap(self):
        self.assertIsNone(executor_v3.retry_budget_violation(_cfg()),
                          "默认 06:30~07:50 的尾部预算必须容得下 RETRY_CAP_SEC")

    def test_shrinking_the_effective_window_below_the_cap_turns_red(self):
        # 边界 = RETRY_CAP_SEC / 0.3 = 2000s；窗口粒度是分钟（60s），故边界两侧取
        # 1980s（尾部预算 594s < 600s）与 2040s（612s ≥ 600s）。首尾缓冲归零，
        # 使 sign_end 直接决定有效窗口宽度。
        cap = executor_v3.RETRY_CAP_SEC
        for width_sec, expect_violation in ((1920, True), (1980, True), (2040, False)):
            with self.subTest(width_sec=width_sec):
                end_min = 6 * 60 + 30 + width_sec // 60
                cfg = _cfg(sign_start=(6, 30), sign_end=(end_min // 60, end_min % 60),
                           edge_front_sec=0, edge_back_sec=0)
                got = executor_v3.retry_budget_violation(cfg)
                if expect_violation:
                    self.assertIsNotNone(got, f"有效窗口 {width_sec}s 应判违规")
                    self.assertIn(str(cap), got, "报错必须点名退避上限")
                    self.assertIn("尾部", got, "报错必须点名尾部预留缓冲")
                else:
                    self.assertIsNone(got, f"有效窗口 {width_sec}s 不应判违规")

    def test_schedule_config_reports_the_violation_loudly(self):
        """设计期入口（配置解析）必须自己出声，不要求调用方先跑一次重试。"""
        env = {
            "YIBAN_SIGN_START": "06:30",
            "YIBAN_SIGN_END": "06:45",   # 900s 有效窗口 ⇒ 尾部预算 270s < 600s
            "YIBAN_WINDOW_EDGE_FRONT_SEC": "0",
            "YIBAN_WINDOW_EDGE_BACK_SEC": "0",
        }
        with mock.patch.dict(os.environ, env, clear=False), \
                self.assertLogs("yiban", level="ERROR") as caught:
            schedule._schedule_config()
        text = "\n".join(caught.output)
        self.assertIn("退避", text, f"窗口过短没有响亮报错：\n{text}")
        self.assertIn("尾部", text, f"报错没有点名尾部预留缓冲：\n{text}")


if __name__ == "__main__":
    unittest.main()
