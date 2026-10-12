# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""前端 .env 行分隔符校验与下拉"未知枚举不静默换值"守卫。

**背景**（与后端 `.env` 行模型同族的另一半）：
- 前端表单对行分隔符**零校验**——含 U+2028/U+0085 的输入可以一路提交到后端；
  后端兜底虽在，但"提交前友好拦截"这一层是空的。
- legacy `components/select-field.js` 的 `paint()` 把**未知枚举值静默换成首项**并回写隐藏
  input：服务器上出现未知 `sign_order` 时，`settings-schedule.js` 的快照 `snap` 记的是
  未知值、而 DOM 被改成首项 ⇒ 下一次**无关保存**会把首项当成"用户改动"写进 `.env`。

**本文件钉住的修复**：
- 常量同源：后端把 `ENV_LINE_BREAK_CHARS` 渲染进页面（`window.YB_ENV_LINE_BREAK_CODES`），
  前端只消费这一份，不另抄一个字符清单；
- `YB.hasEnvLineBreak` / `YB.api` 在写方法提交前拒含换行族的字符串体（提交前拦截）；
- `resolvePaintValue` 对未知值**原样返回**（渲染为未知态），`paint` 不再回写 `input.value`。

标签：F · 前端与界面守卫
覆盖：前端行分隔符常量与后端同源（模板渲染 + core.js 消费）、写方法体拒换行族（10 字符逐个）、下拉未知枚举不静默换值（纯函数 + paint 不回写隐藏 input）
对应实现：`web/static/js/core.js` 的 `ENV_LINE_BREAK_CODES` / `hasEnvLineBreak` / `envBodyLineBreak` / `api`，`frontend/src/settings/model.js` 的 `resolvePaintValue`（Vue 下拉直接绑定原始值），`web/templates/pages/work_settings.html` 的常量渲染
关键断言：常量清单必须由后端渲染（模板出现 `env_line_break_codes`），前端不得再写死第二份码点数组；`hasEnvLineBreak` 对 10 个分隔符逐字符为真、对普通文本为假；`paint` 函数体内不得出现 `input.value`（静默换值的唯一形态）
依赖：⚠ **需要 node 真跑**——`hasEnvLineBreak` / `resolvePaintValue` 抽出后交给 node 执行，`shutil.which("node")` 取不到时整类 `skipUnless`；另读模板与 JS 源码文本
"""
import os
import re
import shutil
import subprocess
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE_JS = os.path.join(BASE, "web", "static", "js", "core.js")
# 设置页迁到 Vue 后，自研 select-field.js 退役；"未知枚举不静默换值"的口径函数搬到
# frontend/src/settings/model.js 的 resolvePaintValue（Vue 下拉直接绑定原始值，不经映射层）。
SELECT_JS = os.path.join(BASE, "frontend", "src", "settings", "model.js")
SETTINGS_TPL = os.path.join(BASE, "web", "templates", "pages", "work_settings.html")
NODE = shutil.which("node")

ALL_BREAK_CODES = [0x0A, 0x0B, 0x0C, 0x0D, 0x1C, 0x1D, 0x1E, 0x85, 0x2028, 0x2029]


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _extract_function(src, name):
    """按花括号配对抽出 `function <name>(...) { ... }` 整段。

    测试专用、够用即可——刻意不做通用 JS 解析。已知脆弱（改动被测 JS 时若命中即抛错、
    不会静默抓错段，故失效方向是红不是假绿）：
    - 定位靠字面量 `"function <name>("`：目标若被格式化（`function name (`、箭头函数、
      对象属性式 `name: function(`）或该字面量在更早的注释/字符串里先出现过，会 ValueError
      或抓错段——被抽函数须保持这一书写形式；
    - 只数 `{`/`}`、不数 `()`/`[]`：字符串/模板串/注释里的裸花括号被计入，会提前或延后闭合；
    - 参数默认值带对象解构（`function f(a, {b}=…)`）时，首个 `{` 落在参数表内、配对起点即偏。
    """
    start = src.index("function " + name + "(")
    i = src.index("{", start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError("函数 %s 未找到匹配的右花括号" % name)


def _run_node(body):
    proc = subprocess.run([NODE, "-e", body], capture_output=True, text=True,
                          timeout=30, encoding="utf-8")
    assert proc.returncode == 0, f"node 执行失败:\n{proc.stderr}"
    return proc.stdout.strip()


class FrontendConstantSourceTest(unittest.TestCase):
    """常量与后端同源：由后端渲染进页面，前端不写死第二份码点清单。"""

    def test_template_renders_backend_constant_into_page(self):
        tpl = _read(SETTINGS_TPL)
        self.assertIn("YB_ENV_LINE_BREAK_CODES", tpl,
                      "设置页未把后端行分隔符清单渲染给前端")
        self.assertIn("env_line_break_codes", tpl,
                      "模板须消费后端下发的 env_line_break_codes（不得前端写死码点）")

    def test_core_reads_rendered_constant(self):
        core = _read(CORE_JS)
        self.assertIn("window.YB_ENV_LINE_BREAK_CODES", core,
                      "core.js 须读取后端渲染的常量（同源）")

    def test_write_api_rejects_body_with_line_break(self):
        core = _read(CORE_JS)
        self.assertIn("function hasEnvLineBreak(", core)
        self.assertIn("function envBodyLineBreak(", core)
        # 写方法（非 GET）提交前必须过这道守卫
        self.assertRegex(core, r'if\s*\(\s*req\.method\s*!==\s*"GET"\s*\)',
                         "YB.api 须对非 GET 请求做行分隔符前置校验")


@unittest.skipUnless(NODE, "需要 node 真跑前端纯函数（取不到则整类 skip）")
class HasLineBreakBehaviorTest(unittest.TestCase):
    def test_all_ten_separators_detected_and_plain_text_passes(self):
        codes_assign = re.search(r"^\s*var ENV_LINE_BREAK_CODES = .*$",
                                 _read(CORE_JS), re.M)
        self.assertIsNotNone(codes_assign, "core.js 未找到 ENV_LINE_BREAK_CODES 定义")
        fn = _extract_function(_read(CORE_JS), "hasEnvLineBreak")
        harness = (
            "var window = { YB_ENV_LINE_BREAK_CODES: %s };\n%s\n%s\n"
            "var out = [];\n"
            "for (var c of %s) { out.push(hasEnvLineBreak('A' + String.fromCharCode(c) + 'B')); }\n"
            "out.push(hasEnvLineBreak('正常单行 https://a/b?c=1'));\n"
            "out.push(hasEnvLineBreak(''));\n"
            "console.log(JSON.stringify(out));\n"
            % (ALL_BREAK_CODES, codes_assign.group(0), fn, ALL_BREAK_CODES)
        )
        got = _run_node(harness)
        vals = [v == "true" for v in got.strip("[]").split(",")]
        self.assertEqual(vals[:10], [True] * 10,
                         f"10 个行分隔符须逐个判真，实际 {got}")
        self.assertEqual(vals[10:], [False, False], "普通单行文本与空串不得误伤")


@unittest.skipUnless(NODE, "需要 node 真跑前端纯函数（取不到则整类 skip）")
class PaintUnknownEnumTest(unittest.TestCase):
    def test_resolve_keeps_unknown_value(self):
        fn = _extract_function(_read(SELECT_JS), "resolvePaintValue")
        harness = (
            "%s\n"
            "var opts = [{ v: 'sequence', t: '顺序' }, { v: 'random', t: '随机' }];\n"
            "var u = resolvePaintValue(opts, 'weird_mode');\n"
            "var k = resolvePaintValue(opts, 'random');\n"
            "console.log(JSON.stringify([u.value, u.hit === null, k.value, k.hit && k.hit.v]));\n"
            % fn
        )
        got = _run_node(harness)
        self.assertEqual(got, '["weird_mode",true,"random","random"]',
                         "未知枚举必须原样返回（不得换成首项）")

    def test_enum_select_binds_raw_snapshot_value(self):
        """未知枚举的静默换值必须根除：下拉直接绑定由**服务端原值回填**的表单字段。

        legacy 的 `paint()` 会把未知值换成首项并**回写隐藏 input**；迁移到 Vue 后自研控件
        退役、改用 EP el-select，口径变成一条**正向**的可证伪判据：
          · `<el-select>` 的 `v-model` 直接绑定表单字段（排序/分布/推送渠道）；
          · 该表单字段由快照以**原值**回填（`order: s.order` / `dist: s.dist` /
            `type: pushSnap.value.type`），中间没有任何映射层/回退层能把它换成首项。
        若有人重新引入"未知值→首项"的解析，回填处或绑定处必然出现映射调用而在这里失败。
        """
        schedule = _read(os.path.join(os.path.dirname(SELECT_JS), "ScheduleCard.vue"))
        notify = _read(os.path.join(os.path.dirname(SELECT_JS), "NotifyCard.vue"))
        fn = _extract_function(_read(SELECT_JS), "resolvePaintValue")
        self.assertNotIn("opts[0]", fn, "resolvePaintValue 仍取首项做回退 —— 未知枚举的静默换值未根除")
        self.assertNotIn(".value =", fn, "resolvePaintValue 回写了值 —— 未知枚举会被静默换值")
        self.assertNotIn("input.value", fn)
        self.assertIn('v-model="form.order"', schedule, "排序下拉必须直绑表单原值")
        self.assertIn('v-model="form.dist"', schedule, "分布下拉必须直绑表单原值")
        self.assertIn("order: s.order", schedule, "排序须由快照原值回填（无映射层）")
        self.assertIn("dist: s.dist", schedule, "分布须由快照原值回填（无映射层）")
        self.assertIn('v-model="pushForm.type"', notify, "推送渠道下拉必须直绑表单原值")
        self.assertIn("type: pushSnap.value.type", notify, "推送渠道须由快照原值回填（无映射层）")
        for rel, src in (("ScheduleCard.vue", schedule), ("NotifyCard.vue", notify)):
            self.assertNotIn("resolvePaintValue", src,
                             f"{rel} 经映射层改写下拉值 —— 未知枚举会被静默换成首项")


if __name__ == "__main__":
    unittest.main()
