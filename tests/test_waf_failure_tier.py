# -*- coding: utf-8 -*-
"""WAF/挑战解析失败必须落**显式不可重试档**（总尝试 1 + 清会话），判据单一真值源。

标签：B · 调度：领取/队列/执行体
覆盖：classify_failure/_retry_budget 的显式不可重试档（ydclearance 挑战解析失败全部 raise 文案、
   白名单文案、requests 的 "Expecting value:" 非 JSON 文案、protocol 的"无签发方回执"假成功
   拒绝文案——词元真值源同步改钉）、该档总尝试=1 且联动清会话缓存、
   PROBE_HARD_FAIL_RE 从 security 同一来源构造（WAF_KEYWORDS+HARD_FAIL_TOKENS 逐词元在场）、
   WAF_BLOCKED_MESSAGE 维持风控档、网络类失败维持普通档、硬失败不计入凭据熔断。
对应实现：yiban/security.py（档位判据唯一真值源）、yiban/engine/attempts.py（档位与清缓存联动）、
   yiban/engine/probe.py（硬失败判据同源构造）、yiban/fyiban/waf.py（raise 文案是判据的**输入**）。
关键断言：waf.py 的全部 raise 文案经 AST 提取逐条过档位判据，不是手抄清单——数量对不上须同批
   修订本文件；"把输入改坏 ⇒ 判据必须红"的活体反例形态是：任一解析失败消息被改回可重试档
   （>=2）即红。探针正则与档位共用同一批词元，出现第三份手抄清单即红。
依赖：纯标准库 + signin 兼容壳；不联网、不建库。整文件在本机执行，无 skip。
"""
import ast
import io
import os
import re
import unittest

import signin

from yiban import security
from yiban.engine import probe

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 腿②现网真实文案：>2000 拦截页过了按短响应设计的 is_waf_blocked 后 requests .json() 抛
LEG2_NON_JSON_MESSAGE = "Expecting value: line 1 column 1 (char 0)"

CHALLENGE_PARSE_SAMPLE = "ydclearance 挑战解析失败: 未找到挑战函数"


def _waf_raise_messages():
    """AST 取 `yiban/fyiban/waf.py` 全部 RuntimeError raise 文案（文案是输入，不是副本）。"""
    src = io.open(os.path.join(BASE, "yiban", "fyiban", "waf.py"), encoding="utf-8").read()
    msgs = []
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
                and isinstance(node.exc.func, ast.Name) and node.exc.func.id == "RuntimeError"
                and node.exc.args and isinstance(node.exc.args[0], ast.Constant)
                and isinstance(node.exc.args[0].value, str)):
            msgs.append(node.exc.args[0].value)
    return msgs


class ChallengeParseTierTest(unittest.TestCase):
    """档位归一：任一挑战解析/白名单/非 JSON 失败 ⇒ 总尝试 1 + 清会话。"""

    def test_waf_raise_messages_count_pinned(self):
        # 登记口径：14 处"ydclearance 挑战解析失败:"前缀 + 1 处白名单（waf.py:146）
        msgs = _waf_raise_messages()
        self.assertEqual(len(msgs), 15, f"waf.py raise 文案应为 15 处，实得 {len(msgs)}：{msgs}")

    def test_every_waf_raise_message_lands_hard_tier(self):
        for msg in _waf_raise_messages():
            with self.subTest(msg=msg):
                self.assertTrue(security.is_hard_fail_message(msg),
                                "硬失败判据必须以真值源命中每条 raise 文案")
                self.assertEqual(signin.classify_failure(msg), signin.HARD_FAIL_MAX_ATTEMPTS)
                budget, clear_cache = signin._retry_budget(msg)
                self.assertEqual(budget, 1, "显式不可重试档：总尝试必须=1，不得落普通档 3 次")
                self.assertTrue(clear_cache, "硬失败档必须联动清除会话缓存（残片无复用价值）")
                self.assertFalse(signin._is_credential_failure(msg),
                                 "WAF/挑战是环境问题不是凭据问题，不得计入熔断")

    def test_whitelist_message_from_security_table_lands_hard_tier(self):
        # 与 waf.py:146 的 raise 文案同源：_WHITELIST_MESSAGES["ydclearance"] 经 require_fyiban 抛出
        msg = security._WHITELIST_MESSAGES["ydclearance"]
        self.assertEqual(signin.classify_failure(msg), signin.HARD_FAIL_MAX_ATTEMPTS)
        self.assertEqual(signin._retry_budget(msg), (1, True))

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

    def test_probe_regex_hits_every_waf_raise_message(self):
        for msg in _waf_raise_messages():
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
