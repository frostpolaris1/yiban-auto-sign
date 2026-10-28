# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""部署 e2e 的接线守卫（工单 6jk）。

标签：J · 运维：部署/备份/发布
覆盖：AGENTS.md §六 第 16/17 条要求部署流程有 e2e 钉住。工单 6jk 的两条
    e2e 是 `scripts/e2e/deploy-container-e2e.sh`（自动轨）与
    `scripts/e2e/deploy-baremetal-e2e.sh`（发布前手跑项）。
    本文件钉四条不变量：
      ① 两个脚本存在，且 `bash -n` 通过（删掉脚本 ⇒ 红）；
      ② 某条 workflow **真的调用**容器 e2e（注释或 `echo` 里出现不算），
         且该 job 声明超时门（`timeout-minutes`）；
      ③ 裸机 e2e 的「发布前手跑项」登记**显性可见**：脚本打印 MANUAL 行，
         `docs/dev/deploy-e2e.md` 同时收录两条命令与该登记（AGENTS §30）。
      ④ 检查器做活体反例：只有注释 / 只有 `echo` 的合成 workflow 必须判空。
对应实现：`scripts/e2e/deploy-container-e2e.sh`、
    `scripts/e2e/deploy-baremetal-e2e.sh`、`.github/workflows/deploy-gate.yml`、
    `docs/dev/deploy-e2e.md`。
关键断言：读文本 + `bash -n`（**不跑 docker、不调 GitHub API**）。
依赖：bash（`bash -n`）；其余为纯文件读取、无网络。
"""
import io
import os
import re
import shutil
import subprocess
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOWS = os.path.join(BASE, ".github", "workflows")
CONTAINER = os.path.join(BASE, "scripts", "e2e", "deploy-container-e2e.sh")
BAREMETAL = os.path.join(BASE, "scripts", "e2e", "deploy-baremetal-e2e.sh")
DOC = os.path.join(BASE, "docs", "dev", "deploy-e2e.md")
BASH = shutil.which("bash")

CONTAINER_REL = "scripts/e2e/deploy-container-e2e.sh"
BAREMETAL_REL = "scripts/e2e/deploy-baremetal-e2e.sh"

#: 从一行 workflow 文本里取出命令本体：剥掉 YAML 的 `- ` / `run:` 前缀与包裹引号。
_RUN_KEY = re.compile(r"^-?\s*run:\s*(?P<cmd>.*)$")

#: 真调用判据：命令本体**以** shell 解释器或 `./` 开头，紧接脚本路径。
#: 行内出现不算——`echo "bash …"` 不执行任何东西。
_CONTAINER_INVOCATION = re.compile(
    r"^(?:sudo\s+)?(?:bash|sh)\s+(?:\./)?scripts/e2e/deploy-container-e2e\.sh(?:\s|$)"
    r"|^\./(?:\./)?scripts/e2e/deploy-container-e2e\.sh(?:\s|$)")


def _read(path):
    with io.open(path, encoding="utf-8") as f:
        return f.read()


def _command_text(line):
    s = line.strip()
    m = _RUN_KEY.match(s)
    if m:
        s = m.group("cmd").strip()
    return s.strip("'\"").strip()


def _workflows(workflow_dir):
    return sorted(n for n in os.listdir(workflow_dir) if n.endswith((".yml", ".yaml")))


def _workflows_invoking(workflow_dir, pattern):
    """返回真的调用了目标脚本的 workflow 文件名列表。"""
    hits = []
    for name in _workflows(workflow_dir):
        text = _read(os.path.join(workflow_dir, name))
        for raw in text.splitlines():
            if raw.strip().startswith("#"):
                continue
            if pattern.match(_command_text(raw)):
                hits.append(name)
                break
    return hits


def _job_block(text, job):
    """取出 `  <job>:` 到下一个顶层 job 键之间的文本；找不到返回空串。"""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.rstrip() == "  %s:" % job:
            start = i
            break
    if start is None:
        return ""
    out = []
    for line in lines[start + 1:]:
        if re.match(r"^  [A-Za-z0-9_-]+:\s*$", line):
            break
        out.append(line)
    return "\n".join(out)


class DeployE2EScriptsTest(unittest.TestCase):
    def _assert_parses(self, path, rel):
        self.assertTrue(os.path.isfile(path), "缺 %s" % rel)
        if not BASH:
            self.skipTest("bash 不在 PATH：部署 e2e 脚本语法检查跳过（CI/WSL 有 bash）")
        proc = subprocess.run([BASH, "-n", path], stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE, timeout=60)
        self.assertEqual(proc.returncode, 0,
                         "bash -n 失败：%s\n%s" % (rel, proc.stderr.decode("utf-8", "replace")))

    def test_container_script_exists_and_parses(self):
        self._assert_parses(CONTAINER, CONTAINER_REL)

    def test_baremetal_script_exists_and_parses(self):
        self._assert_parses(BAREMETAL, BAREMETAL_REL)


class ContainerE2EWiringTest(unittest.TestCase):
    def test_some_workflow_really_invokes_it(self):
        hits = _workflows_invoking(WORKFLOWS, _CONTAINER_INVOCATION)
        self.assertTrue(hits, "没有 workflow 真的调用 %s（注释或 echo 不算）" % CONTAINER_REL)

    def test_job_declares_timeout(self):
        path = os.path.join(WORKFLOWS, "deploy-gate.yml")
        self.assertTrue(os.path.isfile(path), "缺 .github/workflows/deploy-gate.yml")
        block = _job_block(_read(path), "container-deploy-e2e")
        self.assertTrue(block, "deploy-gate.yml 里没有 container-deploy-e2e job")
        self.assertIn("timeout-minutes:", block, "container-deploy-e2e 没有超时门")


class ManualRegistrationTest(unittest.TestCase):
    def test_doc_lists_both_commands_and_manual_item(self):
        self.assertTrue(os.path.isfile(DOC), "缺 docs/dev/deploy-e2e.md")
        text = _read(DOC)
        self.assertIn(CONTAINER_REL, text, "文档没有容器 e2e 命令")
        self.assertIn(BAREMETAL_REL, text, "文档没有裸机 e2e 命令")
        self.assertIn("发布前手跑项", text, "文档没有登记裸机 e2e 为手跑项（AGENTS §30）")

    def test_baremetal_script_prints_manual_line(self):
        text = _read(BAREMETAL)
        self.assertIn("MANUAL（发布前手跑项）", text,
                      "裸机 e2e 没有在输出里显性登记为手跑项（AGENTS §30）")


class WiringCheckerLiveCounterExampleTest(unittest.TestCase):
    """活体反例：只有注释或 echo 的 workflow 必须判空（防恒真）。"""

    def _scan(self, text):
        tmp = tempfile.mkdtemp(prefix="deploy-e2e-wiring-")
        try:
            with io.open(os.path.join(tmp, "x.yml"), "w", encoding="utf-8") as f:
                f.write(text)
            return _workflows_invoking(tmp, _CONTAINER_INVOCATION)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_comment_only_is_not_an_invocation(self):
        text = "# run: bash %s\n" % CONTAINER_REL
        self.assertEqual([], self._scan(text))

    def test_echo_only_is_not_an_invocation(self):
        text = "    - run: echo \"bash %s\"\n" % CONTAINER_REL
        self.assertEqual([], self._scan(text))

    def test_real_invocation_is_found(self):
        text = "    - run: bash %s\n" % CONTAINER_REL
        self.assertEqual(["x.yml"], self._scan(text))


if __name__ == "__main__":
    unittest.main()
