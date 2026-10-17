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
      ③ 闸门脚本本身语法有效（`bash -n`）；
      ④ 口令文件交接按 README 第 6 节的**权限模型**执行（非 root 走 sudo）——
         GitHub 托管 runner 以非 root 的 runner 用户执行，裸 `chown` 必然失败，
         文件留在 runner 名下（0600）⇒ 容器内 uid 10001 读不到 ⇒ C1b/C2 恒红
         （工单 g828）。判据跑**真函数**、换受控 PATH，不读源码措辞。
    本文件另在 dev-verify 全量与 nightly 里跑：workflow 被删掉时那条独立轨仍会红。
对应实现：`scripts/deploy_gate.sh`、`scripts/gate/handoff.sh`、
    `.github/workflows/deploy-gate.yml`。
关键断言：读文本 + `bash -n` + 跑真函数（**不跑 docker、不调 GitHub API**；PyYAML
    不在运行依赖里，门禁不为一条注释引入新依赖）。活体反例：对同一检查器喂"没有引用
    闸门"与"只在注释里提一句"的合成 workflow，必须判空。
依赖：bash（`bash -n` 与口令交接那一族用 `skipIf`）；其余为纯文件读取、无网络。
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
HANDOFF = os.path.join(BASE, "scripts", "gate", "handoff.sh")
BASH = shutil.which("bash")

#: 从一行 workflow 文本里取出**命令本体**：剥掉 YAML 的 `- ` / `run:` 前缀与包裹引号。
#: `run: |` 块里的命令行没有前缀，取不出就按整行算——那正是命令本体。
_RUN_KEY = re.compile(r"^-?\s*run:\s*(?P<cmd>.*)$")


def _command_text(line):
    s = line.strip()
    m = _RUN_KEY.match(s)
    if m:
        s = m.group("cmd").strip()
    return s.strip("'\"").strip()


#: 闸门的**真调用**判据：命令本体**以** shell 解释器（`bash`/`sh`，可带 `sudo `）或 `./`
#: **开头**，紧接闸门路径。
#:
#: 判据钉"开头"而不是"行内出现"：`echo bash scripts/deploy_gate.sh`（**无引号**）里 `bash `
#: 前恰是空格，用"行内出现"会被它骗过——workflow 根本不执行闸门，守卫却绿（假绿）。带引号
#: 与不带引号两种回显形态都由突变验证实测抓到过，故判据必须钉命令开头。
_GATE_INVOCATION = re.compile(
    r"^(?:sudo\s+)?(?:bash|sh)\s+(?:\./)?scripts/deploy_gate\.sh(?:\s|$)"
    r"|^\./(?:\./)?scripts/deploy_gate\.sh(?:\s|$)")


def _read(path):
    with io.open(path, encoding="utf-8") as f:
        return f.read()


def _workflow_files(workflow_dir):
    return sorted(n for n in os.listdir(workflow_dir) if n.endswith((".yml", ".yaml")))


def _workflows_invoking_the_gate(workflow_dir):
    """返回**真的调用**了部署闸门的 workflow 文件名列表。

    判据三跳：① 该行不是注释行（`#` 开头）；② 取出命令本体（剥 `- `/`run:`/引号）；
    ③ 命令本体**以** `bash`/`sh`/`./` 开头且紧接闸门路径。只出现脚本名（注释里，或
    `echo "…"`／`echo …` 里）不算"被引用"——那是废断言（先例同 tests/test_shared_facts_gate.py
    的两跳审）。
    """
    hits = []
    for name in _workflow_files(workflow_dir):
        text = _read(os.path.join(workflow_dir, name))
        for line in text.splitlines():
            if line.lstrip().startswith("#"):
                continue
            if _GATE_INVOCATION.search(_command_text(line)):
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
        """闸门必须跑 docker compose 的 **build ＋ up -d** 两段实际命令——README「一键部署」
        第 4 步那条命令（`docker compose up -d --build`）。

        钉**实际命令文本**（`dc build` / `dc up -d`），不钉裸词：裸词 `build` 在本文件出现
        11 次（函数名 `dc_up_build`、注释、`--build-arg`），钉它恒真——把两处
        `dc build … && dc up -d` 改成只 `dc up -d`（不再构建镜像）也照样绿。
        """
        text = _read(GATE)
        self.assertRegex(text, r"\bdc build\b",
                         "闸门必须跑 `dc build`（README 首次部署含构建镜像）")
        self.assertRegex(text, r"\bdc up -d\b",
                         "闸门必须跑 `dc up -d`（README 的启动命令）")

    def test_gate_delegates_handoff_and_keeps_no_bare_chown(self):
        """闸门必须调用交接函数，且**自己不再裸 chown 10001:10001**。

        裸 chown 是 g828 的缺陷本体：非 root 宿主上它一定失败。把它留在闸门里
        （哪怕同时保留了交接函数）就还能被误用，故本条一并钉死。
        """
        text = _read(GATE)
        self.assertIn("handoff_to_uid10001", text,
                      "闸门必须调用 handoff_to_uid10001 交接口令文件属主")
        self.assertIsNone(
            re.search(r"(?m)^\s*chown\s+10001:10001\b", text),
            "闸门不得再有裸 chown 10001:10001（非 root 宿主必失败，工单 g828）")

    @unittest.skipIf(BASH is None, "bash 不在 PATH：仅 bash -n 语法检查那条跳过（CI/WSL 有 bash）")
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

    def test_checker_is_not_fooled_by_a_quoted_echo_of_the_name(self):
        """`echo "…deploy_gate.sh"` 只提一句名字，不是执行——不得算命中（突变实测抓到）。"""
        got = self._run(
            "g.yml",
            'jobs:\n  x:\n    steps:\n      - run: echo "see scripts/deploy_gate.sh"\n')
        self.assertEqual(got, [], "带引号回显不算被引用：闸门没被真正执行")

    def test_checker_is_not_fooled_by_an_unquoted_echo_prefix(self):
        """**无引号** `echo bash scripts/deploy_gate.sh`：`bash ` 前恰是空格，按"行内出现"
        判据会被它骗过（假绿：workflow 根本不执行闸门，守卫却绿）。判据必须钉命令**开头**，
        故本条必须判空。"""
        got = self._run(
            "g.yml",
            "jobs:\n  x:\n    steps:\n      - run: echo bash scripts/deploy_gate.sh\n")
        self.assertEqual(got, [], "无引号回显不算被引用：命令不是以 bash 开头")

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


@unittest.skipIf(BASH is None, "bash 不在 PATH：口令交接判据跳过（CI/WSL 有 bash）")
class PassphraseHandoffTest(unittest.TestCase):
    """④ 口令文件交接必须按 README 的权限模型：非 root 走 sudo（工单 g828）。

    README 第 6 节的原命令是 `sudo chown 10001:10001 backup-passphrase`——部署者以
    普通用户操作、chown 要提权。GitHub 托管 runner 以非 root 的 `runner` 用户执行，
    裸 `chown` 一定失败：文件留在 runner 名下（0600），容器内 uid 10001 读不到，
    C1b 与 C2 连红。判据跑**真函数**：用受控 PATH 给出假 `id`/`chown`/`sudo`，
    断言选中哪条分支、返回码是什么。摘掉 sudo 分支 ⇒ 本条红。
    """

    def _run_handoff(self, uid, sudo_mode):
        """在受控 PATH 下跑真函数。sudo_mode: "ok" | "absent" | "deny"。"""
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = os.path.join(tmp, "bin")
            os.makedirs(bin_dir)
            calls = os.path.join(tmp, "calls")
            target = os.path.join(tmp, "pass")
            with io.open(target, "w", encoding="utf-8") as f:
                f.write("gate-backup-pass\n")

            def script(name, body):
                p = os.path.join(bin_dir, name)
                with io.open(p, "w", encoding="utf-8", newline="\n") as f:
                    f.write("#!/bin/sh\n" + body)
                os.chmod(p, 0o755)

            script("id", "echo %d\n" % uid)
            script("chown", 'printf "chown %s\\n" "$*" >> "$HANDOFF_CALLS"\n')
            if sudo_mode == "ok":
                script("sudo", 'printf "sudo %s\\n" "$*" >> "$HANDOFF_CALLS"\n')
            elif sudo_mode == "deny":
                script("sudo", "exit 1\n")

            env = dict(os.environ)
            env["PATH"] = bin_dir
            env["HANDOFF_CALLS"] = calls
            # 98/99 是判据自检码：源不到文件或函数不存在时，**任何**用例都必须红。
            # 少了这层，缺文件时 rc=127 会让"必须非零"的负例假绿。
            r = subprocess.run(
                [BASH, "-c",
                 '. "%s" || exit 98; command -v handoff_to_uid10001 >/dev/null || exit 99; '
                 'handoff_to_uid10001 "%s"' % (HANDOFF, target)],
                env=env, capture_output=True, text=True, timeout=60)
            spilled = ""
            if os.path.exists(calls):
                with io.open(calls, encoding="utf-8") as f:
                    spilled = f.read()
            return r.returncode, spilled

    def test_root_chowns_directly_without_sudo(self):
        """root 宿主直调 chown：这就是本机既有的绿路径，不许被改坏。"""
        rc, calls = self._run_handoff(uid=0, sudo_mode="ok")
        self.assertEqual(rc, 0, "root 下交接必须成功")
        self.assertIn("chown 10001:10001", calls, "root 必须直接 chown 10001:10001")
        self.assertNotIn("sudo", calls, "root 不需要 sudo，不该多绕一层")

    def test_non_root_uses_sudo(self):
        """非 root 宿主（GitHub 托管 runner）必须走 `sudo -n chown`——裸 chown 必失败。"""
        rc, calls = self._run_handoff(uid=1001, sudo_mode="ok")
        self.assertEqual(rc, 0, "有免密 sudo 时交接必须成功")
        self.assertIn("sudo -n chown 10001:10001", calls,
                      "非 root 必须经 sudo 执行 chown（README 第 6 节的形态）")

    def test_non_root_without_usable_sudo_fails_loudly(self):
        """既非 root 也没有可用 sudo ⇒ 必须返回非零，由闸门显性 SKIP（跳过 ≠ 绿）。"""
        for mode in ("absent", "deny"):
            with self.subTest(sudo=mode):
                rc, calls = self._run_handoff(uid=1001, sudo_mode=mode)
                self.assertEqual(
                    rc, 1, "权限模型不允许时必须返回 1（闸门据此显性 SKIP）；"
                           "98/99 说明交接函数本身缺失")
                self.assertEqual(calls, "", "交接未成立时不得留下 chown 调用痕迹")


if __name__ == "__main__":
    unittest.main(verbosity=2)
