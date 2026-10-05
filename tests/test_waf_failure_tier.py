# -*- coding: utf-8 -*-
"""WAF/挑战失败必须落**显式不可重试档**（总尝试 1 + 清会话），判据单一真值源。

标签：B · 调度：领取/队列/执行体
覆盖：classify_failure/_retry_budget 的显式不可重试档（挑战检测命中文案、requests 的
   "Expecting value:" 非 JSON 文案、protocol 的"无签发方回执"假成功拒绝文案——词元真值源
   同步改钉）、该档总尝试=1 且联动清会话缓存、
   PROBE_HARD_FAIL_RE 从 security 同一来源构造（WAF_KEYWORDS+HARD_FAIL_TOKENS 逐词元在场）、
   WAF_BLOCKED_MESSAGE 维持风控档、网络类失败维持普通档、硬失败不计入凭据熔断。
对应实现：yiban/security.py（档位判据唯一真值源）、yiban/engine/attempts.py（档位与清缓存联动）、
   yiban/engine/probe.py（硬失败判据同源构造）、yiban/fyiban/waf.py（挑战失败文案的来源）与
   yiban/fyiban/protocol.py（检测命中即用该文案 raise）。
关键断言：waf 的挑战失败文案（`CHALLENGE_DETECTED_MESSAGE`）逐条过档位判据，且协议层
   真的会 raise 它——求解器删除后，"检测命中→响亮失败"是唯一入口，改坏词元即红。
依赖：纯标准库 + signin 兼容壳；不联网、不建库。整文件在本机执行，无 skip。
"""
import io
import os
import re
import unittest

import signin

from yiban import security
from yiban.engine import probe
from yiban.fyiban import waf as fyiban_waf

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 腿②现网真实文案：>2000 拦截页过了按短响应设计的 is_waf_blocked 后 requests .json() 抛
LEG2_NON_JSON_MESSAGE = "Expecting value: line 1 column 1 (char 0)"


def _waf_fail_messages():
    """waf.py 的挑战失败文案（检测命中由协议层 raise 该常量；求解器已删除）。"""
    return [fyiban_waf.CHALLENGE_DETECTED_MESSAGE]


class ChallengeParseTierTest(unittest.TestCase):
    """档位归一：任一挑战/非 JSON 失败 ⇒ 总尝试 1 + 清会话。"""

    def test_every_waf_fail_message_lands_hard_tier(self):
        for msg in _waf_fail_messages():
            with self.subTest(msg=msg):
                self.assertTrue(security.is_hard_fail_message(msg),
                                "硬失败判据必须以真值源命中挑战失败文案")
                self.assertEqual(signin.classify_failure(msg), signin.HARD_FAIL_MAX_ATTEMPTS)
                budget, clear_cache = signin._retry_budget(msg)
                self.assertEqual(budget, 1, "显式不可重试档：总尝试必须=1，不得落普通档 3 次")
                self.assertTrue(clear_cache, "硬失败档必须联动清除会话缓存（残片无复用价值）")
                self.assertFalse(signin._is_credential_failure(msg),
                                 "WAF/挑战是环境问题不是凭据问题，不得计入熔断")

    def test_protocol_raises_challenge_message(self):
        """协议层必须真的用该文案 raise（检测命中→响亮失败是求解器删除后的唯一入口）。"""
        src = io.open(os.path.join(BASE, "yiban", "fyiban", "protocol.py"),
                      encoding="utf-8").read()
        self.assertIn("fyiban_waf.CHALLENGE_DETECTED_MESSAGE", src)
        self.assertRegex(src, r"raise RuntimeError\(fyiban_waf\.CHALLENGE_DETECTED_MESSAGE\)")

    def test_leg2_non_json_message_lands_hard_tier(self):
        self.assertTrue(security.is_hard_fail_message(LEG2_NON_JSON_MESSAGE))
        self.assertEqual(signin.classify_failure(LEG2_NON_JSON_MESSAGE),
                         signin.HARD_FAIL_MAX_ATTEMPTS)
        self.assertEqual(signin._retry_budget(LEG2_NON_JSON_MESSAGE), (1, True))

    def test_hard_fail_tokens_are_the_single_source(self):
        # 真值源只有一处：attempts 的档位判据、probe 的正则都从 security 这组词元派生。
        # 新增成员必须同族（对同一输入重试必然同果）："无签发方回执"是登录最终认证的
        # 确定性失败（protocol 判据），档位与探针自动共用。
        self.assertEqual(security.HARD_FAIL_TOKENS,
                         ("ydclearance", "Expecting value", "无签发方回执"))
        self.assertFalse(security.is_hard_fail_message(
            "HTTPSConnectionPool(host='oauth.yiban.cn', port=443): Read timed out"))

    def test_blocked_message_stays_risk_tier(self):
        # 归一不改变既有合法判定：短拦截页统一文案仍走风控档（2 次、清缓存）
        budget, clear_cache = signin._retry_budget(security.WAF_BLOCKED_MESSAGE)
        self.assertEqual(budget, signin.RISK_MAX_ATTEMPTS)
        self.assertTrue(clear_cache)

    def test_network_failure_stays_normal_tier(self):
        self.assertEqual(signin.classify_failure("Read timed out"), signin.MAX_ATTEMPTS)
        budget, clear_cache = signin._retry_budget("Read timed out")
        self.assertEqual(budget, signin.MAX_ATTEMPTS)
        self.assertFalse(clear_cache)


class ProbeHardFailSameSourceTest(unittest.TestCase):
    """口径④归一：探针硬失败判据与档位共用同一来源，对解析失败/非 JSON 不再零预警。"""

    def test_probe_regex_hits_every_waf_fail_message(self):
        for msg in _waf_fail_messages():
            with self.subTest(msg=msg):
                self.assertIsNotNone(probe.PROBE_HARD_FAIL_RE.search(msg),
                                     "探针对挑战解析失败必须预警（旧口径零命中是登记缺陷）")

    def test_probe_regex_hits_leg2_message(self):
        self.assertIsNotNone(probe.PROBE_HARD_FAIL_RE.search(LEG2_NON_JSON_MESSAGE))

    def test_probe_regex_terms_derived_not_recopied(self):
        pattern = probe.PROBE_HARD_FAIL_RE.pattern
        for token in (*security.WAF_KEYWORDS, *security.HARD_FAIL_TOKENS):
            with self.subTest(token=token):
                # 派生形态即 `security.hard_fail_pattern()` 的 re.escape 产物
                self.assertIn(re.escape(token), pattern,
                              "探针的 WAF 族词元必须由 security 真值源派生，不得手抄第三份")

    def test_probe_regex_still_hits_legacy_features(self):
        # 归一只做加法：原有硬失败特征逐条在场
        for msg in ("图形验证码", "校本化未授权", "登录失败: 密码错误", "Auth Error",
                    "获取登录入口失败", "最终认证失败", "授权设备"):
            with self.subTest(msg=msg):
                self.assertIsNotNone(probe.PROBE_HARD_FAIL_RE.search(msg))


if __name__ == "__main__":
    unittest.main(verbosity=2)
