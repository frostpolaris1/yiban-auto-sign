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
4. **故障转移开关只留弹窗一个入口**：状态格只报状态徽标；开关在弹窗首位、带字段标题，
   其 payload 字面量与勾选初值分别用代码级断言钉住（注释顶替不了）；浮层行菜单的分隔线
   不带外边距。

四者都是"文案/口径/控件归属"级契约，没有运行时断言可依赖，故直接读源码——与项目既有的
`test_settings_tiers_frontend_parity` 同一手法。
"""

import os
import re
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = os.path.join(BASE, "web", "static", "js", "components", "settings-executors.js")
TPL = os.path.join(BASE, "web", "templates", "pages", "work_settings.html")
CSS = os.path.join(BASE, "web", "static", "css", "app.css")


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


def _strip_comments(src):
    """去掉 `//` 行注释（字符串字面量里的 `//` 不动）。

    断言必须落在**可执行代码**上：同形注释可以满足 assertIn（评审实测：payload 键改坏 +
    注入一行 `// fallback_enable: args.enable`，旧用例仍绿）。
    """
    out = []
    for line in src.splitlines():
        buf = []
        quote = None
        i = 0
        while i < len(line):
            ch = line[i]
            if quote:
                buf.append(ch)
                if ch == "\\" and i + 1 < len(line):
                    buf.append(line[i + 1])
                    i += 2
                    continue
                if ch == quote:
                    quote = None
                i += 1
                continue
            if ch in ('"', "'"):
                quote = ch
                buf.append(ch)
                i += 1
                continue
            if ch == "/" and i + 1 < len(line) and line[i + 1] == "/":
                break
            buf.append(ch)
            i += 1
        out.append("".join(buf))
    return "\n".join(out)


def _code_body(src, name):
    """函数体去掉注释后的文本（断言只认代码，注释顶替不了）。"""
    return _strip_comments(_function_body(src, name))


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


class FallbackStateCellIsStatusOnlyTest(unittest.TestCase):
    """故障转移行的状态格**只报状态**，开关只留弹窗一个入口。

    同一状态两个开关入口属「过度」（不足与过度同样算缺陷），故状态格不再放控件；
    断言只认控件构造的稳定措辞（类名、字段名、门函数名），不锁整句文案。
    """

    def test_state_cell_renders_status_badge_only(self):
        body = _code_body(_read(JS), "stateCell")
        self.assertIn("FB_TEXT[fb.status]", body, "状态格仍要报故障转移状态")
        for forbidden in ('class: "switch"', 'class: "track"', 'type: "checkbox"'):
            self.assertNotIn(forbidden, body,
                             "状态格不得再出现开关控件（%s）——开关只在弹窗里" % forbidden)

    def test_state_cell_has_no_second_write_path(self):
        body = _code_body(_read(JS), "stateCell")
        self.assertNotIn("askPassword", body,
                         "状态格不该有独立的口令门（写入口只有弹窗那一个）")
        self.assertNotIn("fallback_enable", body,
                         "状态格不该再打开关接口（防第二开关回流）")

    def test_pointer_copy_never_comes_back(self):
        self.assertNotIn("点「设置」开启", _strip_comments(_read(JS)),
                         "状态格不放指路文案：开关就在「设置」弹窗里，用户看状态不必再被指挥")


class FallbackSwitchPayloadTest(unittest.TestCase):
    """弹窗开关的读写口径必须落在**可执行代码**上。

    同函数里的注释也含 `fallback_enable` / `fb.enabled` 字样，只 assertIn 关键字会被注释
    满足（评审实测：把 payload 键改坏、保留注释，旧用例仍绿）。故这里断言先剥掉 `//` 注释
    再匹配，注释无法顶替。
    """

    def test_switch_initial_checked_reads_config_flag(self):
        body = _code_body(_read(JS), "openRow")
        self.assertIn("if (fb.enabled === true) swInput.checked = true", body,
                      "勾选初值必须只认配置里的 enabled（这一行是代码，注释与复原语句都顶替不了）")

    def test_submit_posts_the_fallback_payload_literal(self):
        body = _code_body(_read(JS), "submit")
        self.assertRegex(body, r"fallback_enable:\s*args\.enable",
                         "提交必须带 fallback_enable 的 payload 字面量（关键字被注释满足不算）")
        self.assertIn('"/api/scheduler/executors"', body,
                      "开关属于整条接口，不是行接口")

    def test_switch_value_is_one_or_zero(self):
        body = _code_body(_read(JS), "switchArg")
        self.assertIn("swInput.checked ? 1 : 0", body,
                      "开关值由勾选态换算成 1/0，不直接送布尔")

    def test_switch_only_save_skips_the_rows_endpoint(self):
        """只拨开关时不得发空体的行接口：后端对缺 type/proxy/name 的请求回 400。

        行接口请求必须包在 `if (args.proxy || args.name != null)` 里，否则只拨开关的保存会
        先 400、Promise.all 直接 reject——用户被告知失败而配置已经落盘。断言只认代码。
        """
        body = _code_body(_read(JS), "submit")
        guard = "if (args.proxy || args.name != null)"
        self.assertIn(guard, body, "行接口请求必须先判有无 type/proxy/name 要改")
        self.assertLess(body.index(guard), body.index('"/api/scheduler/executors/rows/"'),
                        "行接口请求必须在该判据成立时才加入 steps")


class RowMenuDividerSpacingTest(unittest.TestCase):
    """浮层行菜单的危险项分隔线不带外边距。

    线若带外边距，它两侧的文字间距就比其它菜单项宽（用户看到的「分隔线上下空隙大」）。
    """

    def test_floating_divider_has_no_margin(self):
        css = _read(CSS)
        rules = re.findall(r"\.acct-menu--floating\s+\.dd-divider\s*\{([^}]*)\}", css)
        self.assertEqual(len(rules), 1,
                         "浮层菜单分隔线应恰好一条规则（多一条会互相覆盖，正是要防的回流）")
        self.assertRegex(rules[0], r"margin:\s*0\s*;",
                         "分隔线外边距必须归零，否则线两侧间距又比其它菜单项宽")


class FallbackModalSwitchPlacementTest(unittest.TestCase):
    """行内弹窗里的开关要有字段标题、且是弹窗**第一个字段**。

    没有标题又夹在「设置出口」与「存活」之间时，按接近性原则看不出它属于出口还是这一行；
    排在类型/出口字段之前，它才作为这一行的主状态控件先被读到。
    """

    def _body(self):
        return _code_body(_read(JS), "openRow")

    def test_switch_field_has_a_label(self):
        body = self._body()
        self.assertIn('class: "field-label", text: "故障转移开关"', body,
                      "开关要有与同弹窗其它字段同构的字段标题")
        self.assertIn('swInput.setAttribute("aria-describedby", swHelpId)', body,
                      "读屏关联（aria-describedby）照旧保留")

    def test_switch_field_comes_before_type_and_egress_fields(self):
        body = self._body()
        sw_at = body.index('class: "field-label", text: "故障转移开关"')
        for later in ('text: "名称（留空 = 用默认名）"',
                      'text: "类型"',
                      'text: "当前出口（已脱敏）"',
                      'text: "设置出口（留空 = 不修改）"'):
            self.assertIn(later, body)
            self.assertLess(sw_at, body.index(later),
                            "开关字段要排在「%s」之前（它是这一行的主状态控件）" % later)


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
