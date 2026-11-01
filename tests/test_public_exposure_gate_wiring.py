# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""公开数据红线门的 CI 接线守卫（CLEAN-1）。

# 功能

钉住三件事：

1. `verify` 作业真的调仓内入口脚本 `scripts/dev-verify.sh --ci`。
2. `run_ci` 真的跑 `tests/test_public_exposure_gate.py`（非注释行）。
3. 那条命令在 `if [ "$CI_SCOPE" ... ]` 分支**之外**。

第 3 条是本文件的重点。2026-11-01 实测：红线门早已在
`tests/lane-roster.tsv` 的 fast 行，但 `--ci --scope newstack` 会整段
跳过快集——新栈 PR 因此从不执行它。UX-5 的未登记手机号就是这样进
develop 的。所以「门在仓里」与「门在新栈 PR 上执行」是两件事。

# 归属

测试/门禁面。规则正本：AGENTS.md §十六（46–49）与 §十三（28–30）。

# 口径

只读文本，不起子进程。

配对 `fi` 用 `if`/`fi` 计数，不按缩进找首个同名行：`run_ci` 的 else
分支里还有同缩进的嵌套 `if ... fi`（`skip_policy_check`），按缩进会
配错（2026-11-01 审查实测：门移回分支内仍判绿）。

「不得削弱」按**逻辑命令行**判（续行已拼合），注释行不计。
"""

import io
import os
import re
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CI_YML = os.path.join(BASE, ".github", "workflows", "ci.yml")
ENTRY_SCRIPT = os.path.join(BASE, "scripts", "dev-verify.sh")

GATE_FILE = "tests/test_public_exposure_gate.py"
WIRING_FILE = "tests/test_public_exposure_gate_wiring.py"

_IF = re.compile(r"if\b")
_FI = re.compile(r"fi\b")


def _read(path):
    with io.open(path, encoding="utf-8") as f:
        return f.read()


def _func_body(text, name):
    """抽出 shell 函数的正文；抽不出返回空串（调用方据此拒判）。"""
    marker = name + "() {"
    start = text.find(marker)
    if start < 0:
        return ""
    start = text.find("\n", start) + 1
    # 函数以列 0 的 } 结束；正文里的嵌套花括号都有缩进。
    end = text.find("\n}\n", start)
    if end < 0:
        return ""
    return text[start:end]


def _logical_commands(body):
    """把函数正文切成逻辑命令行：`(起始偏移, 文本, 是否注释)`。

    续行（行尾反斜杠）拼成一条；注释行单独成项，不参与命令判据。
    """
    out = []
    buf = ""
    start = 0
    pos = 0
    for raw in body.splitlines(keepends=True):
        line = raw.rstrip("\n")
        if not buf:
            start = pos
        if not buf and line.lstrip().startswith("#"):
            out.append((pos, line, True))
            pos += len(raw)
            continue
        buf += line
        if buf.rstrip().endswith("\\"):
            buf = buf.rstrip()[:-1].rstrip() + " "
            pos += len(raw)
            continue
        out.append((start, buf, False))
        buf = ""
        pos += len(raw)
    if buf:
        out.append((start, buf, False))
    return out


def _scope_block_end(body):
    """返回 `if [ "$CI_SCOPE"` 配对的 `fi` 行末偏移；找不到返回 -1。

    用 `if`/`fi` 计数，不按缩进：else 分支内还有同缩进的嵌套 if。
    """
    scope = body.find('if [ "$CI_SCOPE"')
    if scope < 0:
        return -1
    line_start = body.rfind("\n", 0, scope) + 1
    depth = 0
    pos = line_start
    for raw in body[line_start:].splitlines(keepends=True):
        line = raw.strip()
        if _IF.match(line):
            depth += 1
        elif _FI.match(line):
            depth -= 1
            if depth == 0:
                return pos + len(raw) - 1
        pos += len(raw)
    return -1


def _gate_command_offset(body):
    """返回第一处**非注释**门命令的起始偏移；没有返回 -1。"""
    for offset, text, is_comment in _logical_commands(body):
        if not is_comment and GATE_FILE in text:
            return offset
    return -1


def _gate_runs_outside_scope_branch(body):
    """判红线门命令是否在 scope 分支之外。"""
    gate = _gate_command_offset(body)
    if gate < 0:
        return False
    scope_end = _scope_block_end(body)
    if scope_end < 0:
        return False
    return gate > scope_end


class GateWiringTest(unittest.TestCase):
    """门必须在 CI 里、在新栈 PR 上也执行。"""

    def setUp(self):
        self.ci = _read(CI_YML)
        self.entry = _read(ENTRY_SCRIPT)
        self.run_ci = _func_body(self.entry, "run_ci")

    def test_verify_job_calls_the_entry_script(self):
        self.assertIn(
            "bash scripts/dev-verify.sh --ci", self.ci,
            "verify 作业必须调仓内入口脚本（否则跑法有第二个入口）")

    def test_run_ci_really_runs_the_gate(self):
        self.assertGreater(
            len(self.run_ci), 100,
            "从入口脚本里抽不出 run_ci 函数体——结构改了？本条会因此变废断言")
        self.assertGreater(
            _gate_command_offset(self.run_ci), -1,
            "run_ci 必须真跑公开数据红线门（写在注释里不算）")

    def test_gate_runs_outside_the_scope_branch(self):
        end = _scope_block_end(self.run_ci)
        self.assertGreater(end, -1, "找不到 if [ \"$CI_SCOPE\" ] 的配对 fi")
        self.assertTrue(
            _gate_runs_outside_scope_branch(self.run_ci),
            "红线门必须在 if [ \"$CI_SCOPE\" ] 分支之外：newstack/shared 两种 "
            "scope 也必须执行（否则新栈 PR 静默跳过）")

    def test_gate_command_is_not_neutralized(self):
        commands = [(o, t) for o, t, c in _logical_commands(self.run_ci)
                    if not c and GATE_FILE in t]
        self.assertTrue(commands, "run_ci 里没有非注释的门命令")
        for _, command in commands:
            for bad in ("|| true", "-k ", "--ignore", "--deselect"):
                self.assertNotIn(bad, command,
                                 "红线门不得用 %r 削弱" % bad)

    def test_wiring_test_runs_with_the_gate(self):
        commands = [t for _, t, c in _logical_commands(self.run_ci)
                    if not c and GATE_FILE in t]
        self.assertTrue(commands, "run_ci 里没有非注释的门命令")
        self.assertTrue(
            any(WIRING_FILE in c for c in commands),
            "接线守卫必须与门同一步跑——否则守卫自己也会被静默移除")


class GatePlacementTest(unittest.TestCase):
    """给接线判据装牙。

    反例按真实 `run_ci` 的形状造：else 分支里有**同缩进**的嵌套
    `if ... fi`。按缩进配对会在这里配错（2026-11-01 审查实证）。
    """

    OUTSIDE = (
        'if [ "$CI_SCOPE" = "newstack" ]; then\n'
        "    echo skip\n"
        "else\n"
        "    if ! skip_policy_check x 1; then\n"
        "        exit 1\n"
        "    fi\n"
        "fi\n"
        '    "$py" -m pytest tests/test_public_exposure_gate.py \\\n'
        "        tests/test_public_exposure_gate_wiring.py -q\n"
    )
    INSIDE = (
        'if [ "$CI_SCOPE" = "newstack" ]; then\n'
        "    echo skip\n"
        "else\n"
        "    if ! skip_policy_check x 1; then\n"
        "        exit 1\n"
        "    fi\n"
        '    "$py" -m pytest tests/test_public_exposure_gate.py \\\n'
        "        tests/test_public_exposure_gate_wiring.py -q\n"
        "fi\n"
    )

    def test_outside_is_accepted(self):
        self.assertTrue(_gate_runs_outside_scope_branch(self.OUTSIDE))

    def test_inside_with_nested_if_is_rejected(self):
        self.assertFalse(_gate_runs_outside_scope_branch(self.INSIDE),
                         "同缩进的嵌套 if/fi 不得让门命令蒙混过关")

    def test_comment_only_mention_is_rejected(self):
        body = (
            'if [ "$CI_SCOPE" = "newstack" ]; then\n'
            "    echo skip\n"
            "fi\n"
            "# 见 tests/test_public_exposure_gate.py\n"
        )
        self.assertFalse(_gate_runs_outside_scope_branch(body))

    def test_missing_gate_is_rejected(self):
        self.assertFalse(_gate_runs_outside_scope_branch("echo nothing\n"))


if __name__ == "__main__":
    unittest.main()
