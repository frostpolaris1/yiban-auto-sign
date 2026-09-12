# -*- coding: utf-8 -*-
"""前端 `YB.maskEmail` 与后端 `_mask_email` 的脱敏口径对拍（真实行为，非静态扫描）。

## 为什么要对拍

两端各有一份邮箱脱敏：`web/app.py` 的 `_mask_email`（出站即脱敏）与
`web/static/js/core.js` 的 `maskEmail`（渲染层幂等脱敏）。二者是同一口径的**两份实现**，
只要一边改动而另一边没跟上，就会出现「后端已脱敏、前端再脱一次」或反过来的错位，
表现为域名被吞或完整性泄漏。静态扫描只能证明函数存在，证明不了**行为一致**。

## 本文件怎么测

照 `tests/test_web_logs_date_validation.py` 的做法：从源码里按花括号配对抽出
`function maskEmail(e) { ... }` 交给 node 执行；后端侧抽出 `def _mask_email(e):` 的
函数体后 `exec` 成可调用对象。对同一批输入逐例比对两端返回值，覆盖正常邮箱、
已含 `*`（幂等）、无 `@`、`@` 在首位、本地部长度 1/2/3/超长等边界。

node 不可用时跳过（本套件其余部分不引入硬性 node 依赖）。
"""

import json
import os
import shutil
import subprocess
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE_JS = os.path.join(BASE, "web", "static", "js", "core.js")
APP_PY = os.path.join(BASE, "web", "app.py")
NODE = shutil.which("node")

# (说明, 输入) —— 两端对同一输入必须给出同一结果
CASES = (
    ("正常邮箱", "user@example.com"),
    ("本地部长度 1", "a@example.com"),
    ("本地部长度 2", "ab@example.com"),
    ("本地部长度 3", "abc@example.com"),
    ("本地部超长（仍只保留前 3）", "verylonglocalpart@example.com"),
    ("已含 *（幂等）", "use***@example.com"),
    ("星号在本地部中间", "a*b@example.com"),
    ("无 @", "not-an-email"),
    ("@ 在首位", "@example.com"),
    ("空串", ""),
    ("短域名", "x@q.io"),
)


def _extract_js_function(src, name):
    """按花括号配对抽出 `function <name>(...) { ... }` 整段。"""
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
    raise AssertionError("JS 函数 %s 未找到匹配的右花括号" % name)


def _extract_python_function(src, name):
    """抽出 `def <name>(...):` 及其缩进函数体，exec 后返回可调用对象。

    后端函数体只含 str/find/min 等纯逻辑，无副作用，故可安全整体 exec。
    """
    lines = src.split("\n")
    start = None
    for idx, line in enumerate(lines):
        if line.startswith("def " + name + "("):
            start = idx
            break
    if start is None:
        raise AssertionError("Python 函数 %s 未找到" % name)
    body = [lines[start]]
    for line in lines[start + 1:]:
        if line.strip() == "" or line[:1] in (" ", "\t"):
            body.append(line)
        else:
            break
    namespace = {}
    exec("\n".join(body), namespace)
    return namespace[name]


class MaskEmailParityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(CORE_JS, encoding="utf-8") as fh:
            js_src = fh.read()
        with open(APP_PY, encoding="utf-8") as fh:
            py_src = fh.read()
        cls.js_fn = _extract_js_function(js_src, "maskEmail")
        # staticmethod：否则函存为类属性后经 self.py_fn 访问会被当作绑定方法，多传一个 self
        cls.py_fn = staticmethod(_extract_python_function(py_src, "_mask_email"))

    def _run_js(self, inputs):
        """在 node 里对一批字符串执行抽出的 maskEmail，返回结果列表。"""
        script = (
            self.js_fn
            + "\nvar cases = " + json.dumps(inputs) + ";\n"
            + "console.log(JSON.stringify(cases.map(function (s) { return maskEmail(s); })));\n"
        )
        proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raise AssertionError("node 执行失败：%s" % (proc.stderr or proc.stdout))
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_backend_and_frontend_agree_on_all_cases(self):
        """两端对全部边界输入返回同一脱敏结果。"""
        inputs = [s for _label, s in CASES]
        front = self._run_js(inputs)
        back = [self.py_fn(s) for s in inputs]
        problems = []
        for (label, value), f, b in zip(CASES, front, back):
            if f != b:
                problems.append(
                    "  %s（输入 %r）：前端 %r != 后端 %r" % (label, value, f, b)
                )
        if problems:
            self.fail(
                "YB.maskEmail 与 _mask_email 脱敏口径不一致（单边改动会导致错位脱敏）：\n"
                + "\n".join(problems)
            )

    def test_mask_is_idempotent_on_both_sides(self):
        """两端对已脱敏值必须原样返回（幂等），否则会二次吞字符。"""
        masked = [self.py_fn(value) for _label, value in CASES]
        front = self._run_js(masked)
        for value, f in zip(masked, front):
            self.assertEqual(f, value, "前端 maskEmail 对已脱敏值 %r 不幂等（返回 %r）" % (value, f))
            self.assertEqual(self.py_fn(value), value, "后端 _mask_email 对 %r 不幂等" % value)


if __name__ == "__main__":
    unittest.main(verbosity=2)
