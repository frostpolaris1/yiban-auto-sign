# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""执行体分区的口径说明，用源级断言钉住。

1. **「平均每执行体分到的人数」= 计入容量的账号数 ÷ 并行执行体数**（只数「并行」行：停用与
   故障转移不分担账号）。旧口径是「用户容量上限 ÷ 并行行数」——按名额填满估算，与实际账号数
   无关，用户看到 500 时以为那是"每执行体分到的人数"。同页「设定的账号容量上限」也必须是
   **账号**上限（执行体分的是账号），不是用户上限。
2. **故障转移行的状态文案**：`off` 必须以"开关"为主语（「未启用」），旧文案「未开启故障转移」
   既能读成"功能没启用"，也能读成"执行体没启动"，用户第一次看到分不清去哪一栏找原因。
3. **执行体一览的三处口径句**（见末尾 `ExecutorListWordingTest`）：故障转移行占号导致并行行
   跳号、单并行行时行内出口不生效、以及「单执行体」与账号页「上次实领」的对照。

三者都是"文案/口径"级契约，没有运行时断言可依赖，故直接读源码——与项目既有的
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


class FallbackInlineSwitchTest(unittest.TestCase):
    """状态列要给**开关本体**，不是一句指路文案——否则用户看到状态还得找入口。

    断言只认控件构造与交互链路的稳定措辞（类名、字段名、门函数名），不锁整句文案。
    """

    def test_state_cell_renders_a_real_switch(self):
        body = _function_body(_read(JS), "stateCell")
        self.assertIn('class: "switch"', body, "故障转移行状态列必须渲染出开关本体")
        self.assertIn('class: "track"', body, "开关要与弹窗那个同构（漏了 track 就不是同一个控件）")
        self.assertIn('type: "checkbox"', body, "开关必须带 checkbox 输入")
        self.assertIn("fb.enabled === true", body,
                      "勾选状态只认配置里的 enabled（status 是运行期实况，两者会分叉）")

    def test_state_cell_switch_walks_the_password_gate(self):
        body = _function_body(_read(JS), "stateCell")
        self.assertIn("askPassword", body, "拨动开关要走口令门")
        self.assertIn("fallback_enable", body, "复用整条接口已有的开关字段，不新造接口字段")

    def test_pointer_copy_never_comes_back(self):
        self.assertNotIn("点「设置」开启", _read(JS),
                         "开关已进状态列，那句指路文案不该回流")


class FallbackStatusCopyTest(unittest.TestCase):
    def test_off_state_names_the_switch_not_a_process(self):
        js = _read(JS)
        # 只禁止它出现在**状态映射的值**里：注释里引用旧文案说明改动原因是允许的
        self.assertNotRegex(js, r':\s*"未开启故障转移"',
                            "旧文案把「功能没启用」与「执行体没启动」说成了一件事")
        self.assertIn('off: "未启用"', js)
        self.assertIn('declared_not_running: "已启用·未运行"', js)


class ExecutorListWordingTest(unittest.TestCase):
    """执行体一览新增的三处口径句（纯文案，无运行时断言可依赖，故读源码钉住）。

    断言只挑**稳定的措辞锚点**（不依赖整句、不锁标点）：口径句被改写/删掉即报红，
    避免"下次被顺手改回去也没有东西拦"。
    """

    def test_fallback_row_occupies_a_number_in_doc_and_banner(self):
        """故障转移行占号 → 并行行跳号属正常：卡头 ⓘ 与「添加执行体」成功横幅两处都要说。"""
        tpl = _read(TPL)
        self.assertIn("故障转移行也占一个编号", tpl, "卡头 ⓘ 缺「占号」口径句")
        self.assertIn("不影响运行", tpl, "跳号只解释成“正常”，不能说成“删过行”的结果")
        self.assertIn("就是它在占号", tpl, "缺与「删过行」区分的判据")
        # 绝对归因（"不代表删过行"）与紧邻的"槽位号只增不复用"自相矛盾，不许回流
        self.assertNotIn("不代表删过行", tpl)
        body = _function_body(_read(JS), "addRow")
        self.assertIn("故障转移行也占一个编号", body, "添加成功横幅缺「占号」口径句")

    def test_doc_links_single_executor_and_guards_single_row_hint(self):
        """「单执行体」＝清单只有一行「并行」时的显示名（账号页「上次实领」），且单行有出口提示。"""
        tpl = _read(TPL)
        self.assertIn("「单执行体」不是清单类型", tpl, "缺「单执行体」与清单类型的对照")
        self.assertIn("上次实领", tpl, "对照句必须点明显示在账号页哪一列")
        hint = _function_body(_read(JS), "singleRowHint")
        self.assertIn("只有一个并行执行体时", hint, "单行弹窗缺出口提示")
        self.assertIn("按行生效", hint, "提示必须说明行内出口何时生效")
        self.assertIn("env_keys", hint, "提示里的配置键名仍须取自接口下发，不硬编码")


if __name__ == "__main__":
    unittest.main()
