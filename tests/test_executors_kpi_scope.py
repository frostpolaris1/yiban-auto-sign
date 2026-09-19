# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""执行体分区的两处口径（用户 2026-09-18 裁决），用源级断言钉住。

1. **「平均每执行体分到的人数」= 计入容量的账号数 ÷ 并行执行体数**（只数「并行」行：停用与
   故障转移不分担账号）。旧口径是「用户容量上限 ÷ 并行行数」——按名额填满估算，与实际账号数
   无关，用户看到 500 时以为那是"每执行体分到的人数"。同页「设定的账号容量上限」也必须是
   **账号**上限（执行体分的是账号），不是用户上限。
2. **故障转移行的状态文案**：`off` 必须以"开关"为主语（「未启用」），旧文案「未开启故障转移」
   既能读成"功能没启用"，也能读成"执行体没启动"，用户第一次看到分不清去哪一栏找原因。

两处都是"文案/口径"级契约，没有运行时断言可依赖，故直接读源码——与项目既有的
`test_settings_tiers_frontend_parity` 同一手法。
"""

import os
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = os.path.join(BASE, "web", "static", "js", "components", "settings-executors.js")
TPL = os.path.join(BASE, "web", "templates", "pages", "work_settings.html")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _function_body(src, name):
    """截出 `function <name>(...) { ... }` 的函数体（到下一个顶层 `  }` 为止）。

    断言只针对函数体：注释里引用旧口径说明改动原因是允许的，用整文件 assertNotIn 会误伤
    （写完第一版就被自己的注释命中过一次）。
    """
    start = src.index("function " + name + "(")
    end = src.index("\n  }", start)
    return src[start:end]


class KpiScopeTest(unittest.TestCase):
    def setUp(self):
        self.js = _read(JS)
        self.tpl = _read(TPL)

    def test_average_uses_accounts_counted_not_users_quota(self):
        body = _function_body(self.js, "paintKpis") + _function_body(self.js, "capacityNum")
        self.assertIn('capacityNum("accounts")', body,
                      "平均每执行体的分子必须是「计入容量的账号数」")
        self.assertIn('capacityNum("accounts_max")', body,
                      "容量卡必须是「账号容量上限」")
        self.assertNotIn("users_max", body,
                         "KPI 计算不该再用用户容量上限（执行体分的是账号）")

    def test_average_divides_by_parallel_rows_only(self):
        """分母只数「并行」行——用后端 workers.configured，不自己按清单长度算。"""
        body = _function_body(self.js, "paintKpis")
        self.assertIn("workers.configured", body)
        self.assertIn("Math.ceil(acc / w)", body)

    def test_capacity_card_label_names_accounts(self):
        self.assertIn("设定的账号容量上限", self.tpl)
        self.assertNotIn("设定的容量总人数", self.tpl)

    def test_info_text_states_the_new_scope(self):
        self.assertIn("计入容量的账号数", self.tpl,
                      "卡头口径说明必须写明分子来自哪里")
        self.assertIn("不是用户上限", self.tpl,
                      "必须点明与用户上限的区别（此前正是这两者被混用）")

    def test_first_card_is_todays_progress(self):
        """首卡从「清单行数」换成「今日进度」：行数在表里一眼可见，卡片该回答"今天跑得怎么样"。"""
        self.assertIn("今日进度", self.tpl)
        self.assertNotIn("清单行数", self.tpl)
        body = _function_body(self.js, "paintKpis") + _function_body(self.js, "progressText")
        self.assertIn('"set-exec-kpi-progress"', body)
        self.assertIn("activity.totals", body, "分子必须取当日已了结数")
        self.assertIn("current_accounts", body, "分母必须取计入容量的账号数")


class FallbackSwitchHintTest(unittest.TestCase):
    """「未启用」必须同时给出开关在哪——否则用户看到状态却找不到入口。"""

    def test_off_state_points_to_the_row_dialog(self):
        js = _read(JS)
        self.assertRegex(js, r'fb\.status === "off"[\s\S]{0,200}点「设置」开启',
                         "未启用时要在状态列指出开关位置")


class FallbackStatusCopyTest(unittest.TestCase):
    def test_off_state_names_the_switch_not_a_process(self):
        js = _read(JS)
        # 只禁止它出现在**状态映射的值**里：注释里引用旧文案说明改动原因是允许的
        self.assertNotRegex(js, r':\s*"未开启故障转移"',
                            "旧文案把「功能没启用」与「执行体没启动」说成了一件事")
        self.assertIn('off: "未启用"', js)
        self.assertIn('declared_not_running: "已启用·未运行"', js)


if __name__ == "__main__":
    unittest.main()
