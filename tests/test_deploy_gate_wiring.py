# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""发布判据的守卫：部署闸门必须被一条自动轨引用，且跑的是 README 的形态（工单 c9uz）。

标签：J · 运维：部署/备份/发布
覆盖：AGENTS.md §六 第 16/17 条要求「全新干净环境按 README 从零装成、一次完成、零手动
    调整」，且「部署流程必须有 e2e 钉住（干净容器内按 README 走通、服务健康检查通过）」。
    那条 e2e 的唯一载体是 `scripts/deploy_gate.sh`。此前它被 `.github/` 与 `tests/`
    引用 **0 次**——可以整个失败而无人红，唯一的"守卫"是有人记得手跑。本文件钉三条
    不变量：
      ① 部署闸门被某条 CI workflow 引用（删掉那条 job ⇒ 本文件红）；
      ② 闸门的默认 pip 索引是**空**（走 README 的默认索引），且它跑的是 docker compose
         的 build+up（README「一键部署」那条命令的形态）——不是另一条同义命令；
      ③ 闸门脚本本身语法有效（`bash -n`）。
    本文件另在 dev-verify 全量与 nightly 里跑：workflow 被删掉时那条独立轨仍会红。
对应实现：`scripts/deploy_gate.sh`、`.github/workflows/deploy-gate.yml`。
关键断言：读文本 + `bash -n`（**不跑 docker、不调 GitHub API**；PyYAML 不在运行依赖里，
    门禁不为一条注释引入新依赖）。活体反例：对同一检查器喂"没有引用闸门"与"只在注释里
    提一句"的合成 workflow，必须判空。
依赖：bash（仅 `bash -n` 那条用 `skipIf`）；其余为纯文件读取、无网络、无 skip。
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
GATE = os.path.join(BASE, "scripts", "deploy_gate.sh")
BASH = shutil.which("bash")

#: 闸门的**真调用**形态：前面是 shell 解释器（`bash`/`sh`）或 `./`，即它真的被当命令
#: 执行。`echo "scripts/deploy_gate.sh"` 这类"只是提一句名字"不得算命中——否则删掉真
#: 调用、留一句回显也能骗过守卫（突变验证实测抓到过这一形态）。
_GATE_INVOCATION = re.compile(r"(?:^|\s)(?:bash\s+|sh\s+|\./)(?:\./)?scripts/deploy_gate\.sh(?:\s|$)")


def _read(path):
    with io.open(path, encoding="utf-8") as f:
        return f.read()


def _workflow_files(workflow_dir):
    return sorted(n for n in os.listdir(workflow_dir) if n.endswith((".yml", ".yaml")))


def _workflows_invoking_the_gate(workflow_dir):
    """返回**真的调用**了部署闸门的 workflow 文件名列表。

    判据两跳：① 该行不是注释行（`#` 开头）；② 该行把脚本当命令执行（前面是 bash/sh 或
    `./`）。只出现脚本名（注释里、或 `echo "…"` 里）不算"被引用"——那是废断言（先例同
    tests/test_shared_facts_gate.py 的两跳审）。
    """
    hits = []
    for name in _workflow_files(workflow_dir):
        text = _read(os.path.join(workflow_dir, name))
        for line in text.splitlines():
            if line.lstrip().startswith("#"):
                continue
            if _GATE_INVOCATION.search(line):
                hits.append(name)
                break
    return hits


class DeployGateWiringTest(unittest.TestCase):
    """① 部署闸门被某条 CI workflow 引用；② 默认索引是 README 的默认；③ 语法有效。"""

    def test_a_workflow_invokes_the_deploy_gate(self):
        """没有这条引用，发布判据（README 干净部署）就无人守（工单 c9uz 关闭条件 1）。"""
        hits = _workflows_invoking_the_gate(WORKFLOWS)
        self.assertTrue(
            hits,
            "没有 workflow 引用 scripts/deploy_gate.sh —— 发布判据（README 干净部署）"
            "无人守：闸门可整个失败而无人红，唯一的守卫是有人记得手跑（工单 c9uz）")

    def test_gate_default_pip_index_is_the_readme_default(self):
        """闸门缺省**不传** PIP_INDEX_URL（走默认索引），与 README 的
        `docker compose up -d --build` 同形态。国内镜像旁路由 caller 显式传，
        不得写回缺省——写回即"跑的是另一条同义命令"（工单 c9uz 关闭条件 2）。"""
        text = _read(GATE)
        self.assertRegex(
            text, r'GATE_PIP_INDEX_URL="\$\{GATE_PIP_INDEX_URL:-\}"',
            "GATE_PIP_INDEX_URL 的缺省必须是空（README 的默认索引形态）；把它写回某个"
            "镜像地址 = 闸门跑的是另一条同义命令，不再是 README 那条")

    def test_gate_runs_the_readme_docker_compose_command(self):
        """闸门必须跑 docker compose 的 build+up——README「一键部署」第 4 步那条命令。"""
        text = _read(GATE)
        self.assertIn("up -d", text, "闸门必须跑 docker compose up -d（README 的部署命令）")
        self.assertIn("build", text, "闸门必须跑 docker compose build（README 首次部署含构建）")

    @unittest.skipIf(BASH is None, "bash 不在 PATH")
    def test_gate_script_is_syntactically_valid(self):
        """语法坏了闸门跑不起来——CI job 会红，这里给出更早、更响的信号。"""
        r = subprocess.run([BASH, "-n", GATE], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, "scripts/deploy_gate.sh 语法错误：%s" % r.stderr)


class WiringCheckerLiveCounterExampleTest(unittest.TestCase):
    """守卫不承重就是废的：检查器必须对"没有引用闸门"与"只在注释里提一句"判空。"""

    def _run(self, filename, content):
        with tempfile.TemporaryDirectory() as tmp:
            with io.open(os.path.join(tmp, filename), "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
            return _workflows_invoking_the_gate(tmp)

    def test_checker_catches_a_real_invocation(self):
        got = self._run("g.yml", "jobs:\n  x:\n    steps:\n      - run: bash scripts/deploy_gate.sh\n")
        self.assertEqual(got, ["g.yml"], "真调用必须被抓住")

    def test_checker_ignores_a_workflow_without_the_gate(self):
        got = self._run("other.yml", "jobs:\n  x:\n    steps:\n      - run: echo hi\n")
        self.assertEqual(got, [], "没引用闸门的 workflow 不得算命中")

    def test_checker_is_not_fooled_by_a_comment_only_mention(self):
        """注释里写个脚本名不算"被引用"——否则删掉真调用、留一句注释也能骗过守卫。"""
        got = self._run(
            "g.yml",
            "# 见 scripts/deploy_gate.sh\njobs:\n  x:\n    steps:\n      - run: echo hi\n")
        self.assertEqual(got, [], "注释行不算被引用（两跳审）")

    def test_checker_is_not_fooled_by_an_echo_of_the_name(self):
        """`echo "…deploy_gate.sh"` 只提一句名字，不是执行——不得算命中（突变实测抓到）。"""
        got = self._run(
            "g.yml",
            'jobs:\n  x:\n    steps:\n      - run: echo "see scripts/deploy_gate.sh"\n')
        self.assertEqual(got, [], "回显脚本名不算被引用：闸门没被真正执行")

    def test_checker_accepts_the_canonical_forms(self):
        """合法调用形态：`bash …` / `sh …` / `./…` 都算；`run: |` 块里的缩进行也算。"""
        for body in ("bash scripts/deploy_gate.sh",
                     "sh scripts/deploy_gate.sh",
                     "./scripts/deploy_gate.sh",
                     "bash scripts/deploy_gate.sh --phase A",
                     "run_block"):
            with tempfile.TemporaryDirectory() as tmp:
                if body == "run_block":
                    content = "jobs:\n  x:\n    steps:\n      - run: |\n          bash scripts/deploy_gate.sh\n"
                else:
                    content = "jobs:\n  x:\n    steps:\n      - run: %s\n" % body
                with io.open(os.path.join(tmp, "g.yml"), "w",
                             encoding="utf-8", newline="\n") as f:
                    f.write(content)
                self.assertEqual(_workflows_invoking_the_gate(tmp), ["g.yml"],
                                 "合法调用形态 %r 必须被抓住" % body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
