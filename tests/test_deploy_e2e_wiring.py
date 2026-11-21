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


#: 自动触发事件：push 或 pull_request。只有 workflow_dispatch 不算自动轨。
_AUTO_TRIGGER = re.compile(r"^\s{2}(push|pull_request)\s*:", re.M)


def _on_block(text):
    """取出 `on:` 的块内容（到下一个顶格键为止）；找不到返回空串。"""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.rstrip() in ("on:", "'on':", '"on":'):
            out = []
            for follow in lines[i + 1:]:
                if follow.strip() and not follow.startswith((" ", "\t")):
                    break
                out.append(follow)
            return "\n".join(out)
    return ""


def _has_auto_trigger(text):
    """workflow 是否至少有一个自动触发事件（push / pull_request）。"""
    return bool(_AUTO_TRIGGER.search(_on_block(text)))


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

    def test_invoking_workflow_has_automated_trigger(self):
        """调用容器 e2e 的 workflow 必须至少有一个自动触发事件。

        只剩 `workflow_dispatch` 时，job 还在、命令还在，但推送不会触发它：
        自动轨静默消失（AGENTS §30）。本测钉住 push / pull_request 之一在场。
        """
        hits = _workflows_invoking(WORKFLOWS, _CONTAINER_INVOCATION)
        self.assertTrue(hits, "没有 workflow 真的调用 %s" % CONTAINER_REL)
        automated = [n for n in hits if _has_auto_trigger(_read(os.path.join(WORKFLOWS, n)))]
        self.assertTrue(automated,
                        "调用容器 e2e 的 workflow 只有 workflow_dispatch：自动触发被删也不会红")


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

    def test_workflow_dispatch_only_is_not_automated(self):
        text = "on:\n  workflow_dispatch:\n"
        self.assertFalse(_has_auto_trigger(text),
                         "只有 workflow_dispatch 被误判为自动轨")

    def test_pull_request_trigger_is_automated(self):
        text = "on:\n  workflow_dispatch:\n  pull_request:\n    branches: [develop]\n"
        self.assertTrue(_has_auto_trigger(text),
                        "pull_request 触发未被识别为自动轨")


if __name__ == "__main__":
    unittest.main()


# ---- 一键更新失败矩阵（U2）的接线守卫 --------------------------------------
# 判据：R1..R10 必须有一条**真调用**的自动轨，且 job 有超时门；
# 环境起不来时 workflow 必须显性 SKIP（写进 step summary），不许静默当绿。

UPDATE_ROLLBACK = os.path.join(BASE, "scripts", "e2e", "update-rollback-e2e.sh")
UPDATE_ROLLBACK_REL = "scripts/e2e/update-rollback-e2e.sh"

_UPDATE_ROLLBACK_INVOCATION = re.compile(
    r"^(?:sudo\s+)?(?:bash|sh)\s+(?:\./)?scripts/e2e/update-rollback-e2e\.sh(?:\s|$)"
    r"|^\./(?:\./)?scripts/e2e/update-rollback-e2e\.sh(?:\s|$)")


class UpdateRollbackE2EWiringTest(unittest.TestCase):
    """U2 一键更新的失败矩阵必须在自动轨上，且 SKIP 不许冒充通过。"""

    def test_script_exists_and_parses(self):
        if not BASH:
            self.skipTest("bash 不在 PATH：更新演练脚本语法检查跳过（CI/WSL 有 bash）")
        self.assertTrue(os.path.isfile(UPDATE_ROLLBACK), "缺 %s" % UPDATE_ROLLBACK_REL)
        proc = subprocess.run([BASH, "-n", UPDATE_ROLLBACK],
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)
        self.assertEqual(proc.returncode, 0,
                         "bash -n 失败：%s\n%s" % (UPDATE_ROLLBACK_REL,
                                                  proc.stderr.decode("utf-8", "replace")))

    def test_some_workflow_really_invokes_it(self):
        hits = _workflows_invoking(WORKFLOWS, _UPDATE_ROLLBACK_INVOCATION)
        self.assertTrue(hits, "没有 workflow 真的调用 %s（注释或 echo 不算）" % UPDATE_ROLLBACK_REL)
        automated = [n for n in hits if _has_auto_trigger(_read(os.path.join(WORKFLOWS, n)))]
        self.assertTrue(automated, "调用更新演练的 workflow 只有 workflow_dispatch")

    def test_job_declares_timeout_and_runs_dry_run_suite(self):
        path = os.path.join(WORKFLOWS, "deploy-gate.yml")
        block = _job_block(_read(path), "update-rollback-e2e")
        self.assertTrue(block, "deploy-gate.yml 里没有 update-rollback-e2e job")
        self.assertIn("timeout-minutes:", block, "update-rollback-e2e 没有超时门")
        # 没有 docker 的机器上也必须有一条真跑的命令：update.sh 干跑契约。
        self.assertIn("go test", block, "job 没有跑 update.sh 干跑契约")
        self.assertIn("TestUpdateSh", block, "干跑命令没点名 TestUpdateSh")

    def test_environment_skip_is_explicit(self):
        path = os.path.join(WORKFLOWS, "deploy-gate.yml")
        block = _job_block(_read(path), "update-rollback-e2e")
        self.assertTrue(block, "deploy-gate.yml 里没有 update-rollback-e2e job")
        # 环境错误（脚本 rc=2）才允许 SKIP：判定必须钉在 -eq 2 上，
        # 不是「任何失败都可以跳过」。
        self.assertIn('"$rc" -eq 2', block, "SKIP 必须只对脚本 rc=2（环境错误）生效")
        self.assertIn("SKIP", block, "环境起不来时没有显性 SKIP")
        self.assertIn("GITHUB_STEP_SUMMARY", block, "SKIP 没有写进 step summary（不算留痕）")
        self.assertIn("不是通过", block, "SKIP 必须写明「不算通过」，不能冒充绿")
        # 真失败（rc≠0/2）必须原样传出，不许被 SKIP 分支吞掉。
        self.assertIn('exit "$rc"', block, "job 没有把真失败的退出码传出去")

#: pg 客户端提取时列出的库名**只能是 DT_NEEDED 名**（`libX.so.N`），
#: 因为 `tar -ch` 解引用后成员名就是请求名，二进制的 DT_NEEDED 也只认这个名字。
#: 多列一条 `libX.so.N.M` 会让 tar 在镜像升级 soname 后 Cannot stat 而整步判死。
_PG_LIBS_LINE = re.compile(r"^[ \t]*PG_LIBS=.*$", re.M)
_PG_LIB_TOKEN = re.compile(r"^[^\s]+/lib[^/\s]+\.so\.\d+$")


def _pg_lib_tokens(text):
    """取出脚本里 PG_LIBS 各行声明的库名（剥掉引号与续行拼接）。"""
    chunks = []
    for line in _PG_LIBS_LINE.findall(text):
        _, _, rhs = line.partition("=")
        rhs = rhs.strip().strip('"').strip("'")
        # 续行写法 \`PG_LIBS="$PG_LIBS lib/..."\`：累积变量本身不是库名
        rhs = re.sub(r"^\$\{?PG_LIBS\}?\s+", "", rhs)
        chunks.append(rhs)
    joined = " ".join(chunks)
    return [t for t in joined.split() if t]


class PgClientExtractionListTest(unittest.TestCase):
    """H5 提取清单不许把「镜像里的 soname 次版本」当成「PG 主版本」写死。

    2026-11-17T20:19Z postgres:18 镜像重建后 libpq 由 5.18 升到 5.19，
    旧写法 libpq.so.5.${PG_MAJOR} 遂 Cannot stat，tar 非零退出，
    update-rollback-e2e 自 2026-11-21T08:30Z 起全红。守卫钉两条：
      ① 每个库名都是 DT_NEEDED 形（以 .so.N 收尾，不带主版本替换）；
      ② 清单里不许出现任何 ${...} 展开（库名不接受变量拼接）。
    """

    def setUp(self):
        self.script = _read(UPDATE_ROLLBACK)

    def test_libs_are_dt_needed_names(self):
        tokens = _pg_lib_tokens(self.script)
        self.assertTrue(tokens, "脚本里没有 PG_LIBS 声明，守卫失去落点")
        bad = [t for t in tokens if not _PG_LIB_TOKEN.match(t)]
        self.assertEqual(bad, [], "PG_LIBS 里混进了非 DT_NEEDED 名：%s" % bad)

    def test_libs_never_expand_a_variable(self):
        tokens = _pg_lib_tokens(self.script)
        self.assertTrue(tokens, "脚本里没有 PG_LIBS 声明，守卫失去落点")
        expanded = [t for t in tokens if "$" in t]
        self.assertEqual(expanded, [],
                         "库名不许用变量拼接（库的 soname 次版本与 PG 主版本无关）：%s" % expanded)

    def test_guard_catches_the_regression(self):
        # 活体反例：把历史上那条写回去，两条守卫都必须报出来。
        broken = self.script.replace(
            'PG_LIBS="lib/x86_64-linux-gnu/libpq.so.5"',
            'PG_LIBS="lib/x86_64-linux-gnu/libpq.so.5 lib/x86_64-linux-gnu/libpq.so.5.' + chr(36) + '{PG_MAJOR}"')
        self.assertNotEqual(broken, self.script, "反例没替换成功，守卫等于没测")
        tokens = _pg_lib_tokens(broken)
        self.assertTrue([t for t in tokens if not _PG_LIB_TOKEN.match(t)],
                        "守卫漏了写死 soname 的反例")
        self.assertTrue([t for t in tokens if chr(36) in t],
                        "守卫漏了变量展开的反例")
