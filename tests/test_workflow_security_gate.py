# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""工作流供应链与 shell 直插的安全契约。

标签：J · 运维：部署/备份/发布
覆盖 .github/workflows/ 下三条不变量：
  ① 所有 uses: 都是完整 40 位 commit SHA（可变 tag 即供应链入口）；
  ② 已退役的自动触发 workflow（signin.yml、带 Gitee 私钥的 mirror.yml）不得回潮；
  ③ run 段内直插 ${{ github.base_ref }} / ${{ github.event.before }} 必须判红
     （这两个表达式先由 GitHub 展开成裸文本再交给 shell，不经过 shell 转义）。
关键断言：纯静态读文本；每条检查器都有活体反例，注释掉执行行必须响亮变红。
依赖：只读文件 + 本机 bash；无网络、无第三方库。
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
INTERP_GATE = os.path.join(BASE, "scripts", "workflow-run-interp-gate.sh")
BASH = shutil.which("bash")

#: uses: owner/repo@ref；ref 允许 40 位十六进制（SHA）或任意可变量（tag/分支）。
_USES = re.compile(r"^\s*-?\s*uses:\s*([^\s#]+)\s*(#.*)?$", re.M)
#: 40 位小写十六进制
_SHA = re.compile(r"^[0-9a-f]{40}$")


def _read(path):
    with io.open(path, encoding="utf-8") as f:
        return f.read()


def _workflow_files():
    return sorted(n for n in os.listdir(WORKFLOWS) if n.endswith((".yml", ".yaml")))


def _unpinned_refs(text):
    """返回该文本里所有不是 40 位 SHA 的 uses: 引用。"""
    bad = []
    for match in _USES.finditer(text):
        ref = match.group(1).split("@")[-1]
        if not _SHA.match(ref):
            bad.append(match.group(1))
    return bad


class WorkflowShaPinTest(unittest.TestCase):
    """① 所有 uses: 固定到完整 commit SHA（既有纪律，不得被新 job 破例）。"""

    def test_every_workflow_pins_uses_to_a_full_sha(self):
        files = _workflow_files()
        self.assertTrue(files, ".github/workflows 为空，SHA 固定门不得扫空当绿")
        bad = {}
        for name in files:
            offenders = _unpinned_refs(_read(os.path.join(WORKFLOWS, name)))
            if offenders:
                bad[name] = offenders
        self.assertEqual(bad, {},
                         "workflow 的 uses 必须固定 40 位 commit SHA（可变 tag 即供应链入口）："
                         f"{bad}")

    def test_the_pin_checker_catches_a_mutable_tag(self):
        """活体反例：同一检查器对合成内容必须判红——否则这条断言可能整个是废的。"""
        offender = _unpinned_refs("jobs:\n  x:\n    steps:\n      - uses: acme/act@v1\n")
        self.assertEqual(offender, ["acme/act@v1"],
                         "检查器必须能抓住可变 tag")

    def test_the_pin_checker_accepts_a_sha_pinned_ref(self):
        sha = "3d3c42e5aac5ba805825da76410c181273ba90b1"
        self.assertEqual(_unpinned_refs(f"      - uses: actions/checkout@{sha} # v7\n"), [])


class RetiredWorkflowGuardTest(unittest.TestCase):
    """② M42：已退役的自动触发 workflow 不得回潮。"""

    #: develop 上已删除（commit 7c7ccf7），回潮即重新引入两个审查发现
    RETIRED = ("signin.yml", "mirror.yml")

    def test_retired_signin_and_mirror_workflows_are_absent(self):
        present = [n for n in self.RETIRED if os.path.isfile(os.path.join(WORKFLOWS, n))]
        self.assertEqual(present, [],
                         f"这些 workflow 已退役（见 commit 7c7ccf7）：{present}")
        # main 分支上仍有它们（默认分支治理未完成）——本仓检出基于 develop，
        # 这里钉的是「别在 develop 上加回来」，不是 main 的现状。

    def test_retired_server_web_branch_is_not_a_trigger(self):
        """旧部署分支 server-web 已退役；任何自动触发都不得再挂它。"""
        files = _workflow_files()
        self.assertTrue(files, "workflow 目录为空，分支检查会空转")
        offenders = [n for n in files if "server-web" in _read(os.path.join(WORKFLOWS, n))]
        self.assertEqual(offenders, [],
                         "server-web 已随旧栈退役，不得再出现在 workflow 触发面：%s" % offenders)

    def test_no_workflow_references_a_gitee_private_key(self):
        """mirror.yml 的危害面是带 Gitee 私钥；任何 workflow 都不得再引这个 secret。"""
        files = _workflow_files()
        self.assertTrue(files, "workflow 目录为空，私钥检查会空转")
        offenders = []
        for name in files:
            text = _read(os.path.join(WORKFLOWS, name))
            if "GITEE_PRIVATE_KEY" in text or "gitee/frostpolaris" in text:
                offenders.append(name)
        self.assertEqual(offenders, [],
                         "不得有 workflow 引用 Gitee 私钥/推送到 Gitee：%s" % offenders)


class WorkflowRunInterpGateTest(unittest.TestCase):
    """③ P1-6：run 段内直插表达式必须判红。

    这两个表达式先由 GitHub 展开成裸文本再交给 shell，不经过 shell 转义；取值来自
    base 分支名与 commit sha，属外部输入。正确形态是 job/step 的 env: 映射成
    BASE_REF / BEFORE_SHA，shell 内以带引号的变量使用。门接在
    server/scripts/verify.sh（stacks 车道），并登记进 tooling-gate 的 shellcheck 面。
    """

    def _run(self, workflows):
        if not BASH:
            self.skipTest("bash 不在 PATH")
        tmp = tempfile.mkdtemp(prefix="wf-interp-")
        try:
            for name, text in workflows.items():
                with io.open(os.path.join(tmp, name), "w", encoding="utf-8") as fh:
                    fh.write(text)
            return subprocess.run(
                [BASH, INTERP_GATE, tmp], cwd=BASE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_real_workflows_are_clean(self):
        if not BASH:
            self.skipTest("bash 不在 PATH")
        proc = subprocess.run([BASH, INTERP_GATE], cwd=BASE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=120)
        self.assertEqual(
            proc.returncode, 0,
            "run 段仍有直插：%s" % proc.stderr.decode("utf-8", "replace"))

    def test_direct_interpolation_inside_run_block_is_red(self):
        fixture = "\n".join([
            "name: t",
            "on: [pull_request]",
            "jobs:",
            "  a:",
            "    runs-on: ubuntu-latest",
            "    steps:",
            "      - name: classify",
            "        run: |",
            '          if [ -n "${{ github.base_ref }}" ]; then',
            '            git fetch origin "${{ github.base_ref }}"',
            "          fi",
        ])
        proc = self._run({"a.yml": fixture})
        self.assertEqual(proc.returncode, 1, "run 块内直插没有被判红")
        err = proc.stderr.decode("utf-8", "replace")
        self.assertIn("a.yml:", err, "命中没有按 file:line 报出来")
        self.assertIn("github.base_ref", err)

    def test_inline_run_value_is_red(self):
        fixture = "\n".join([
            "jobs:",
            "  a:",
            "    runs-on: ubuntu-latest",
            "    steps:",
            '      - run: git fetch origin "${{ github.event.before }}"',
        ])
        proc = self._run({"b.yml": fixture})
        self.assertEqual(proc.returncode, 1, "run: 行内直插没有被判红")
        self.assertIn(b"github.event.before", proc.stderr)

    def test_env_indirection_is_green(self):
        fixture = "\n".join([
            "jobs:",
            "  a:",
            "    runs-on: ubuntu-latest",
            "    env:",
            "      BASE_REF: ${{ github.base_ref }}",
            "      BEFORE_SHA: ${{ github.event.before }}",
            "    steps:",
            "      - name: classify",
            "        run: |",
            '          if [ -n "$BASE_REF" ]; then',
            '            git fetch --no-tags --depth=1 origin "$BASE_REF"',
            "          fi",
            '          base="$BEFORE_SHA"',
        ])
        proc = self._run({"c.yml": fixture})
        self.assertEqual(
            proc.returncode, 0,
            "env: 间接引用被误判：%s" % proc.stderr.decode("utf-8", "replace"))

    def test_expression_outside_run_is_not_shell(self):
        fixture = "\n".join([
            "jobs:",
            "  a:",
            "    runs-on: ubuntu-latest",
            '    if: github.base_ref != ""',
            "    steps:",
            "      - name: doc",
            "        with:",
            '          note: "${{ github.base_ref }}"',
            "        run: echo ok",
        ])
        proc = self._run({"d.yml": fixture})
        self.assertEqual(proc.returncode, 0, "if:/with: 不进 shell，不该判红")

    def test_gate_is_wired_into_verify_and_tooling_gate(self):
        with io.open(os.path.join(BASE, "server", "scripts", "verify.sh"),
                     encoding="utf-8") as fh:
            verify = fh.read()
        calls = [ln for ln in verify.splitlines()
                 if "workflow-run-interp-gate.sh" in ln
                 and not ln.strip().startswith("#")]
        self.assertTrue(calls, "server/scripts/verify.sh 没有真调用 workflow-run-interp-gate.sh")
        with io.open(os.path.join(BASE, "scripts", "tooling-gate.sh"),
                     encoding="utf-8") as fh:
            tooling = fh.read()
        block = re.search(r"(?ms)^SHELL_FILES=\(\n(.*?)^\)", tooling)
        self.assertIsNotNone(block, "tooling-gate.sh 里找不到 SHELL_FILES=(...) 数组")
        self.assertRegex(
            block.group(1),
            r"(?m)^\s*scripts/workflow-run-interp-gate\.sh\s*$",
            "新脚本没登记进 tooling-gate 的 SHELL_FILES 数组（注释行不算）",
        )


class ShellFilesRegistrationTest(unittest.TestCase):
    """发布/配置脚本必须进 tooling-gate 的 shellcheck 扫描面。"""

    REQUIRED = (
        "scripts/release/make-manifest.sh",
        "scripts/release/sign.sh",
        "scripts/release/verify.sh",
        "scripts/check-git-config.sh",
    )

    def test_release_and_config_scripts_are_registered(self):
        with io.open(os.path.join(BASE, "scripts", "tooling-gate.sh"),
                     encoding="utf-8") as fh:
            tooling = fh.read()
        block = re.search(r"(?ms)^SHELL_FILES=\(\n(.*?)^\)", tooling)
        self.assertIsNotNone(block, "tooling-gate.sh 里找不到 SHELL_FILES=(...) 数组")
        for path in self.REQUIRED:
            self.assertRegex(
                block.group(1),
                r"(?m)^\s*%s\s*$" % re.escape(path),
                "%s 没登记进 tooling-gate 的 SHELL_FILES" % path,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
