# -*- coding: utf-8 -*-
"""WAF/挑战失败必须落**显式不可重试档**（总尝试 1 + 清会话），判据单一真值源。

标签：B · 调度：领取/队列/执行体
覆盖：classify_failure/_retry_budget 的显式不可重试档（挑战检测命中文案、requests 的
   "Expecting value:" 非 JSON 文案、protocol 的"无签发方回执"假成功拒绝文案——词元真值源
   同步改钉）、该档总尝试=1 且联动清会话缓存、
   PROBE_HARD_FAIL_RE 从 security 同一来源构造（WAF_KEYWORDS+HARD_FAIL_TOKENS 逐词元在场）、
   WAF_BLOCKED_MESSAGE 维持风控档、网络类失败维持普通档、硬失败不计入凭据熔断、
   ASCII 词元收紧后三条消费腿（响应体判定/重试档位/探针判据）同判据、WAF 族在 attempts.py
   只有一个名单来源。
   风控信号（`executor_v3._is_risk_signal`）只共用 WAF 族那一半；凭据族文案在那里必须为假
   （工单 `yiban-auto-sign-zggs`）。
对应实现：yiban/security.py（档位判据唯一真值源）、yiban/engine/attempts.py（档位与清缓存联动）、
   yiban/engine/probe.py（硬失败判据同源构造）、yiban/engine/executor_v3.py（风控信号的第二个
   读者）、yiban/challenge.py（挑战失败文案与形态判据的来源）与
   yiban/platform.py（检测命中即用该文案 raise）。
关键断言：waf 的挑战失败文案（`CHALLENGE_DETECTED_MESSAGE`）逐条过档位判据，且协议层
   真的会 raise 它——求解器删除后，"检测命中→响亮失败"是唯一入口，改坏词元即红。
依赖：纯标准库 + signin 兼容壳；不联网、不建库。整文件在本机执行，无 skip。
"""
import io
import os
import re
import unittest

import signin

from yiban import challenge as yiban_challenge
from yiban import security
from yiban.engine import probe

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 腿②现网真实文案：>2000 拦截页过了按短响应设计的 is_waf_blocked 后 requests .json() 抛
LEG2_NON_JSON_MESSAGE = "Expecting value: line 1 column 1 (char 0)"
#: **不含 WAF 词元**的挑战形态消息：用来单独钉 `_is_risk_signal` 的挑战形态那一腿。
#: 不能用 `CHALLENGE_DETECTED_MESSAGE`——它自带 "WAF" 字样，挑战腿被摘掉时它照样为真
#: （实测：该断言在突变 M3 下绿，属空转守卫）。
CHALLENGE_ONLY_MESSAGE = "<html>ydclearance</html>"


def _waf_fail_messages():
    """waf.py 的挑战失败文案（检测命中由协议层 raise 该常量；求解器已删除）。"""
    return [yiban_challenge.CHALLENGE_DETECTED_MESSAGE]


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
        src = io.open(os.path.join(BASE, "yiban", "platform.py"),
                      encoding="utf-8").read()
        self.assertIn("challenge.CHALLENGE_DETECTED_MESSAGE", src)
        self.assertRegex(src, r"raise RuntimeError\(challenge\.CHALLENGE_DETECTED_MESSAGE\)")

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


class WafTokenBoundaryTierTest(unittest.TestCase):
    """词元收紧后的档位联动：真 WAF 文案照旧入档，base64 撞出的 aWAFb 一律不入档。

    同一批词元有三条消费腿（工单 `yiban-auto-sign-u21x` 的盘查结论）：响应体判定
    `is_waf_blocked`、重试档位 `classify_failure`、探针硬失败判据 `PROBE_HARD_FAIL_RE`。
    收紧必须三条腿同时生效——只改一条就是"同批改一半"。

    风控信号是**第四条腿**，但只共用 WAF 族那一半：它的语义是"平台在限我们"，
    与档位（"别浪费重试"）相反，故不并凭据族（工单 `yiban-auto-sign-zggs`）。
    """

    #: 纯 base64 形态的失败消息：含 aWAFb，不含任何中文词元，也不含硬失败词元。
    BASE64_MESSAGE = ("MIHbMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDBC7aWAFbrWLsOSBrj57z0KDgq"
                      "I2dvq7iKq6CZgXp2GvS5RufTg2d3L4A2fvWEMeH5y2LRkfPxBheozagMaKWfgd")

    def test_probe_regex_does_not_hit_base64_message(self):
        self.assertIsNone(probe.PROBE_HARD_FAIL_RE.search(self.BASE64_MESSAGE),
                          "探针对 base64 里的 aWAFb 不得报硬失败（假预警）")

    def test_probe_regex_still_hits_real_waf_messages(self):
        for msg in (*_waf_fail_messages(), security.WAF_BLOCKED_MESSAGE,
                    LEG2_NON_JSON_MESSAGE, "上游返回 Request blocked by WAF. ID=7f3a"):
            with self.subTest(msg=msg[:40]):
                self.assertIsNotNone(probe.PROBE_HARD_FAIL_RE.search(msg),
                                     "收紧后真 WAF 文案必须照常预警")

    def test_is_hard_fail_message_applies_the_same_boundary_rule(self):
        """硬失败词元与 WAF 词元同一条匹配规则：两侧字母数字的粘连形态不算命中。"""
        self.assertTrue(security.is_hard_fail_message(yiban_challenge.CHALLENGE_DETECTED_MESSAGE))
        self.assertTrue(security.is_hard_fail_message(LEG2_NON_JSON_MESSAGE))
        self.assertTrue(security.is_hard_fail_message("Set-Cookie: https_ydclearance=abc"))
        self.assertFalse(security.is_hard_fail_message("abcydclearanceX"))

    def test_base64_message_is_not_a_risk_tier(self):
        self.assertEqual(signin.classify_failure(self.BASE64_MESSAGE), signin.MAX_ATTEMPTS,
                         "裸子串把 aWAFb 判成风控类 = 少一次重试并清会话缓存")
        budget, clear_cache = signin._retry_budget(self.BASE64_MESSAGE)
        self.assertEqual((budget, clear_cache), (signin.MAX_ATTEMPTS, False))

    def test_real_risk_messages_stay_in_risk_tier(self):
        for msg in (security.WAF_BLOCKED_MESSAGE, "请求被 WAF 风控拦截",
                    "访问服务禁用", "风险访问，已被拦截"):
            with self.subTest(msg=msg[:30]):
                self.assertEqual(signin.classify_failure(msg), signin.RISK_MAX_ATTEMPTS)

    def test_executor_risk_signal_keeps_the_waf_family(self):
        """风控信号必须命中 WAF 族与挑战形态——改坏这一半，本用例要红。

        `_is_risk_signal` 与档位**不共用**凭据族（见下一条），但 WAF 族那一半仍共用
        （工单 `yiban-auto-sign-zggs` 只切语义相反的那一半）。
        """
        from yiban.engine import executor_v3

        self.assertFalse(executor_v3._is_risk_signal(self.BASE64_MESSAGE),
                         "执行体不得把 aWAFb 当风控信号")
        self.assertTrue(executor_v3._is_risk_signal(security.WAF_BLOCKED_MESSAGE))
        # 中文词元的"长消息"腿不得被收紧削掉（is_waf_blocked 有 2000 上界，这条没有）
        self.assertTrue(executor_v3._is_risk_signal("上游长文" + "正" * 3000 + "风控"))
        # 挑战形态那一腿必须独立在场：前置钉住该消息**不含 WAF 词元**，否则它被 WAF 腿
        # 遮盖、这条断言空转（突变 M3 实测过这个形状）。
        self.assertFalse(security.matches_waf_keywords(CHALLENGE_ONLY_MESSAGE),
                         "前置：本条消息不得含 WAF 词元，否则挑战腿的断言是空转")
        self.assertTrue(executor_v3._is_risk_signal(CHALLENGE_ONLY_MESSAGE),
                        "挑战形态是风控信号，不得漏判")
        # 生产文案必须仍触发信号。它自带 "WAF" 字样，故**由 WAF 腿满足**——钉挑战腿的是
        # 上面那对（前置断言 + CHALLENGE_ONLY_MESSAGE）。本条只钉"线上那句 raise 的文案
        # 不会掉出信号面"（文案改写即红）。
        self.assertTrue(executor_v3._is_risk_signal(
            yiban_challenge.CHALLENGE_DETECTED_MESSAGE),
            "生产用的挑战文案必须仍是风控信号（本条可能由 WAF 腿满足）")

    def test_credential_message_is_not_a_risk_signal(self):
        """凭据类失败**不是**风控信号：判成风控会把整条出口速率砍半（工单 zggs 的根因）。

        生产实证：三条"风控信号回退"日志紧邻的文案全是 `登录失败: 账号或密码错误`，
        fallback 出口被连砍两次降到 1/4。凭据族在重试档位里仍是风控类（`RISK_FAIL_KEYWORDS`
        是有意口径，见 `tests/test_login_e2e_mock.py`）——两处语义相反，必须分开。
        """
        from yiban.engine import executor_v3

        for msg in ("登录失败: 账号或密码错误", "账号或密码错误", "登录失败",
                    "错误尝试过多", "无效的应用端", "e003", "OAuth 页解析失败",
                    "登录响应异常"):
            with self.subTest(msg=msg):
                self.assertFalse(executor_v3._is_risk_signal(msg),
                                 "凭据/环境类失败不是平台限速信号，不得触发出口降速")
        # 反面对照：同一批文案在档位判据里仍是风控类（有意口径，同批不得改掉）
        self.assertEqual(signin.classify_failure("账号或密码错误"),
                         signin.RISK_MAX_ATTEMPTS)

    def test_waf_family_has_one_definition_point(self):
        """WAF 族在 `attempts.py` 只允许引用 security 的名册，不得再写字面量。"""
        src = io.open(os.path.join(BASE, "yiban", "engine", "attempts.py"),
                      encoding="utf-8").read()
        self.assertIn("security.WAF_KEYWORDS", src,
                      "attempts 必须引用 yiban.security 的名册（转发或判据），不得自抄名单")
        for token in security.WAF_KEYWORDS:
            with self.subTest(token=token):
                self.assertNotIn(f'"{token}"', src,
                                 "同一事实两个定义点必然各自演化（工单 u21x 的根因形状）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
