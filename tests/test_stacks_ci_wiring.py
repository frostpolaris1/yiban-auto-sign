# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""新栈入口与 server 脚本可执行位的 CI 接线契约。

标签：J · 运维：部署/备份/发布
覆盖：
  · scripts/verify-stacks.sh 必须由 CI 的 stacks 作业真调；stacks 作业不得内联
    栈命令，入口脚本必须真跑 server 与 ui 两条栈门禁。
  · server/scripts/*.sh 必须带可执行位：入口脚本用 ./ 直接执行它们，
    index 模式 100644 会让整条 stacks 作业以 rc=126 红。
关键断言：钉「命令原文只住入口脚本一份」与「入口脚本直接调用的脚本必须可执行」；
  不钉步骤顺序，不复制命令文本。
依赖：只读文件 + 本机 git；无网络、无第三方库。
"""
import os
import posixpath
import re
import subprocess
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CI_YML = os.path.join(BASE, ".github", "workflows", "ci.yml")
STACKS_ENTRY = os.path.join(BASE, "scripts", "verify-stacks.sh")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _ci_job_body(yml, job):
    """切出 ci.yml 里某个 job 的整段（2 空格缩进的 <job>: 到下一个同级 job）。"""
    m = re.search(r"(?m)^  %s:\s*$" % re.escape(job), yml)
    if m is None:
        return ""
    rest = yml[m.end():]
    nxt = re.search(r"(?m)^  [A-Za-z0-9_-]+:\s*$", rest)
    return rest[:nxt.start()] if nxt else rest


class StacksCiWiringTest(unittest.TestCase):
    """新栈入口 scripts/verify-stacks.sh 必须由 CI 的 stacks 作业真调。

    ci.yml 的注释曾声称「两侧都跑同一个本地入口」，steps 里却内联
    ./scripts/verify.sh 与 npm run verify——注释与执行面分叉，本地入口
    因此没有任何自动轨引用。本类钉两跳：
      ① stacks 作业只调入口脚本，不内联栈命令；
      ② 入口脚本真跑两个栈的验证。
    """

    def setUp(self):
        self.ci = _read(CI_YML)
        self.body = _ci_job_body(self.ci, "stacks")

    def test_stacks_job_exists(self):
        self.assertGreater(len(self.body), 100,
                           "ci.yml 里抽不出 stacks 作业——结构改了？本条会变废断言")

    def test_stacks_job_runs_the_entry_script(self):
        runs = [ln.strip() for ln in self.body.splitlines()
                if ln.strip() == "run: bash scripts/verify-stacks.sh"]
        self.assertEqual(
            len(runs), 1,
            "stacks 作业必须有一条未注释的 run: bash scripts/verify-stacks.sh："
            "否则本地入口没有自动执行点，脚本与 CI 的执行面各说各话")
        self.assertTrue(os.path.isfile(STACKS_ENTRY),
                        "入口脚本 scripts/verify-stacks.sh 不存在")

    def test_stacks_job_does_not_inline_stack_commands(self):
        """内联命令必须消失：命令原文只住入口脚本一份。"""
        # 注释里提到命令不算内联；只看非注释行（去掉行首 # 开头的行）。
        code = "\n".join(ln for ln in self.body.splitlines()
                         if not ln.strip().startswith("#"))
        self.assertNotIn("./scripts/verify.sh", code,
                         "stacks 作业又内联了 server 门禁命令：入口脚本成了摆设")
        self.assertNotIn("npm run verify", code,
                         "stacks 作业又内联了 ui 门禁命令：入口脚本成了摆设")
        self.assertIn("run: npm ci", code,
                      "依赖安装步不见了：入口脚本不负责联网装依赖")

    def test_entry_script_runs_both_stacks(self):
        """两条栈命令必须是未注释的独立执行行（注释掉一行即红）。"""
        # 不能对原文 assertIn：给命令加 # 前缀后原文仍命中，注释洞会放过
        # 「脚本只跑一栈」（复审 D1）。先过滤注释行，再要求整行恰好出现一次。
        lines = [ln.strip() for ln in _read(STACKS_ENTRY).splitlines()
                 if not ln.strip().startswith("#")]
        for cmd, label in (("(cd server && ./scripts/verify.sh)", "server"),
                           ("(cd ui && npm run verify)", "ui")):
            self.assertEqual(
                lines.count(cmd), 1,
                "入口脚本没有未注释地真跑 %s 门禁：%r" % (label, cmd))


class ServerScriptExecBitTest(unittest.TestCase):
    """server/scripts/*.sh 必须带可执行位——入口脚本用 ./ 直接调它们。

    scripts/verify-stacks.sh 的执行行是 (cd server && ./scripts/verify.sh)。
    文件在 index 里是 100644 时该行返回 126（Permission denied），整条 stacks
    作业红：CI run 40745639135（develop d3790676）实测。server/README.md
    把三个脚本都写成直接执行命令（./scripts/verify.sh、
    ./scripts/generate.sh --check、./scripts/license.sh --write），
    故本条按形状钉住整个目录。

    模式取自 git ls-files -s 的 index 位，不看工作区：Windows 工作区没有
    可执行位（os.access(X_OK) 恒真、st_mode 恒无 x），看工作区即哑门。
    """

    #: 入口脚本里「切目录后直接执行」的写法。按形状匹配，不写死 server：
    #: 将来 (cd ui && ./scripts/x.sh) 同类写法也必须被看见（复审 P2）。
    _DIRECT_CALL = re.compile(
        r"cd ([A-Za-z0-9_./-]+) && \./([A-Za-z0-9_./-]+\.sh)")

    def _modes(self, pathspec):
        proc = subprocess.run(
            ["git", "ls-files", "-s", "--", pathspec],
            cwd=BASE, capture_output=True, text=True, timeout=60)
        self.assertEqual(
            proc.returncode, 0,
            "前置就绪：git ls-files 失败（rc=%s, stderr=%r）——本判据无法成立"
            % (proc.returncode, proc.stderr))
        out = proc.stdout
        modes = {}
        for line in out.splitlines():
            meta, path = line.split("\t", 1)
            modes[path] = meta.split()[0]
        return modes

    def test_server_scripts_are_executable(self):
        modes = {p: m for p, m in self._modes("server/scripts/").items()
                 if p.endswith(".sh")}
        self.assertTrue(
            modes, "git ls-files 在 server/scripts/ 下扫不到 .sh——扫空不得当绿")
        non_exec = sorted(p for p, m in modes.items() if m != "100755")
        self.assertEqual(
            non_exec, [],
            "以下脚本缺可执行位（index 模式应为 100755）：%s；"
            "修法：git update-index --chmod=+x <路径>" % non_exec)

    def test_entry_script_targets_are_executable(self):
        """入口脚本里每条 cd <目录> && ./<脚本> 的目标必须可执行。"""
        body = _read(STACKS_ENTRY)
        # 注释行不许算数：把执行行整行注释掉时，这条断言必须消失并让位给
        # StacksCiWiringTest 的红，而不是继续拿注释当证据（复审 P3）。
        code = "\n".join(ln for ln in body.splitlines()
                         if not ln.strip().startswith("#"))
        targets = self._DIRECT_CALL.findall(code)
        self.assertTrue(
            targets, "入口脚本里找不到直接调用的脚本——本条会变废断言")
        for directory, rel in targets:
            path = posixpath.normpath(posixpath.join(directory, rel))
            modes = self._modes(path)
            self.assertTrue(modes, "入口脚本调的 %s 不在库里" % path)
            self.assertEqual(
                modes[path], "100755",
                "入口脚本直接调用 %s，但它没有可执行位"
                "（CI run 40745639135 实测 rc=126）" % path)


if __name__ == "__main__":
    unittest.main()
