# -*- coding: utf-8 -*-
"""`YIBAN_EGRESS_RATE` 只有一个域：名册声明域 ＝ 读取窗口 ＝ 引擎速率域（工单 zx54）。

标签：B · 调度：领取/队列/执行体
覆盖：`config/registry.json` 的 `domain`、`schedule.egress_rate()` 的读取窗口、
   `token_bucket.RATE_MIN/RATE_MAX` 的夹取域——三处必须同值；域外值必须**钳到边界
   并告警**（原值＋钳后值＋来源），不得静默夹取或静默回落。
对应实现：`yiban/engine/schedule.py`（`egress_rate`）、`yiban/engine/token_bucket.py`
   （`RATE_MIN` / `RATE_MAX`）、`config/registry.json`。
关键断言：① 名册域与引擎域逐值相等；② 读取窗口来自同一对常量（把常量改窄，读取
   结果跟着变）；③ 两个方向的域外值都产生 WARNING，且告警带原值与钳后值。
依赖：临时环境变量；不触网、不建库。
"""
import os
import unittest
from unittest import mock

from yiban.adapters.config import loader as config_loader
from yiban.engine import schedule, token_bucket


class EgressRateDomainSingleSourceTest(unittest.TestCase):
    def test_registry_domain_equals_engine_domain(self):
        """① 名册是声明的唯一定义点；它与引擎常量必须逐值相等。"""
        domain = config_loader.domain_of("YIBAN_EGRESS_RATE")
        self.assertEqual(domain, [token_bucket.RATE_MIN, token_bucket.RATE_MAX],
                         "名册域与引擎域是同一个量：不一致就会重现"
                         "[0.01,100] 被静默夹到 [0.2,4.0] 的缺陷")

    def test_reader_follows_the_engine_constants(self):
        """② 读取窗口不是字面量：把引擎常量改窄，读取结果立即跟着变。"""
        with mock.patch.object(token_bucket, "RATE_MIN", 0.5), \
                mock.patch.object(token_bucket, "RATE_MAX", 1.5), \
                mock.patch.dict(os.environ, {"YIBAN_EGRESS_RATE": "3"}), \
                self.assertLogs("yiban", level="WARNING"):
            self.assertEqual(schedule.egress_rate(), 1.5,
                             "上限若写死 100，这里会返回 3 而不是 1.5")

    def test_out_of_domain_value_warns_and_clamps_to_nearest_bound(self):
        """③ 两个方向都必须出声：原值、钳后值、来源。"""
        for raw, want in (("50", token_bucket.RATE_MAX),
                          ("0.05", token_bucket.RATE_MIN)):
            with self.subTest(YIBAN_EGRESS_RATE=raw), \
                    mock.patch.dict(os.environ, {"YIBAN_EGRESS_RATE": raw}), \
                    self.assertLogs("yiban", level="WARNING") as cm:
                got = schedule.egress_rate()
                self.assertAlmostEqual(got, want, places=9)
                text = "\n".join(cm.output)
                self.assertIn(raw, text, "告警必须回显原值")
                self.assertIn(str(want), text, "告警必须回显钳后值")
                self.assertIn("YIBAN_EGRESS_RATE", text, "告警必须点名配置键")
                self.assertIn("进程环境", text, "告警必须点名来源")

    def test_in_domain_value_is_returned_without_warning(self):
        """非空对照：域内值原样返回且不告警（否则上一条可能靠"总是告警"空转）。"""
        with mock.patch.dict(os.environ, {"YIBAN_EGRESS_RATE": "2.0"}), \
                self.assertNoLogs("yiban", level="WARNING"):
            self.assertAlmostEqual(schedule.egress_rate(), 2.0, places=9)


if __name__ == "__main__":
    unittest.main(verbosity=2)
