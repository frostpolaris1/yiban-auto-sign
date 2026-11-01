# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""工具链快门的 CI 接线守卫（CLEAN-7a）。

# 功能

钉住四件事：

1. `ci.yml` 里存在 `tooling-gate` 作业，且没有作业级 `if`（每次 PR 都跑）。
2. 作业真的执行 `bash scripts/tooling-gate.sh`（不是只写在注释里）。
3. 两个外部工具按精确版本安装（actionlint / govulncheck）。
4. 入口脚本在库内存在。

# 归属

测试/门禁面。规则正本：AGENTS.md §28–§30（约束要么有门、要么显式豁免）。

# 口径

只读文本。`_job_block` 用「下一个 2 空格缩进的 job 行」切块；
合成样本在 `JobBlockTest` 里验证切块不会把相邻作业吞进来。
"""

import io
import os
import re
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CI_YML = os.path.join(BASE, ".github", "workflows", "ci.yml")
ENTRY = os.path.join(BASE, "scripts", "tooling-gate.sh")

JOB = "tooling-gate"


def _read(path):
    with io.open(path, encoding="utf-8") as f:
        return f.read()


def _job_block(text, name):
    """抽出 ci.yml 里一个作业的正文；找不到返回空串。"""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line == "  " + name + ":":
            start = i
            break
    if start is None:
        return ""
    out = [lines[start]]
    for line in lines[start + 1:]:
        # 下一个作业以恰好 2 个空格缩进、以冒号结尾。
        if re.match(r"^  [A-Za-z0-9_-]+:\s*$", line):
            break
        out.append(line)
    return "\n".join(out)


class ToolingGateWiringTest(unittest.TestCase):
    """作业存在、无条件、真调入口脚本、工具钉版。"""

    def setUp(self):
        self.ci = _read(CI_YML)
        self.block = _job_block(self.ci, JOB)

    def test_job_exists(self):
        self.assertTrue(self.block, "ci.yml 缺 tooling-gate 作业")

    def test_job_has_no_condition(self):
        self.assertNotRegex(
            self.block, r"(?m)^    if:",
            "tooling-gate 不得带作业级 if：每次 PR 都要跑")

    def test_job_runs_the_entry_script(self):
        self.assertIn(
            "run: bash scripts/tooling-gate.sh", self.block,
            "作业必须真调入口脚本（写在注释里不算）")

    def test_tools_are_pinned(self):
        self.assertRegex(self.block, r"actionlint@v\d+\.\d+\.\d+",
                         "actionlint 必须按精确版本安装")
        self.assertRegex(self.block, r"govulncheck@v\d+\.\d+\.\d+",
                         "govulncheck 必须按精确版本安装")

    def test_entry_script_is_tracked(self):
        self.assertTrue(os.path.isfile(ENTRY),
                        "scripts/tooling-gate.sh 不存在")


class JobBlockTest(unittest.TestCase):
    """给切块判据装牙：相邻作业不得被吞进来。"""

    SAMPLE = (
        "jobs:\n"
        "  first:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: echo one\n"
        "  second:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: echo two\n"
    )

    def test_block_stops_at_next_job(self):
        block = _job_block(self.SAMPLE, "first")
        self.assertIn("echo one", block)
        self.assertNotIn("echo two", block)

    def test_missing_job_is_empty(self):
        self.assertEqual(_job_block(self.SAMPLE, "third"), "")


if __name__ == "__main__":
    unittest.main()
