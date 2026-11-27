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
      ⑤ 一键更新失败矩阵（U2）的接线（P1-6）：required job 保留 update.sh
         干跑契约；需要 `--privileged` systemd 的全矩阵只在**手动、自托管**
         runner 的独立 workflow 上跑；任何非 0 退出判红，不许 rc=2 洗绿。
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
# 判据（P1-6 修正，2026-11）：R1..R10 全矩阵要一次性 systemd 容器
# （docker --privileged + 真 systemd PID1），托管 runner 上不可得。因此分成两条轨：
#   ① required job（deploy-gate.yml 的 update-rollback-e2e）= update.sh
#      **干跑契约**，不依赖 docker，托管 runner 上必定真跑；
#   ② 全矩阵拆到独立 workflow：手动 workflow_dispatch、自托管 runner 标签、
#      权限最小化（只读代码），不再挂托管 runner。
# 两条轨都 fail-closed：任何非 0 退出判红。rc=2→exit 0、::warning、
# continue-on-error、`|| true` 都是假绿——旧口径让「环境起不来」冒充通过。

UPDATE_ROLLBACK = os.path.join(BASE, "scripts", "e2e", "update-rollback-e2e.sh")
UPDATE_ROLLBACK_REL = "scripts/e2e/update-rollback-e2e.sh"
DRY_RUN_JOB = "update-rollback-e2e"

_UPDATE_ROLLBACK_INVOCATION = re.compile(
    r"^(?:sudo\s+)?(?:bash|sh)\s+(?:\./)?scripts/e2e/update-rollback-e2e\.sh(?:\s|$)"
    r"|^\./(?:\./)?scripts/e2e/update-rollback-e2e\.sh(?:\s|$)")

#: 「非 0 洗成绿」的构造：逐条判据 + 失败原因。只看非注释行。
_FALSE_GREEN = (
    (re.compile(r"(?:-eq|-ne|-gt|-ge|-lt|-le)\s+2\b"), "按 rc=2（环境错误）分支跳过"),
    (re.compile(r"^\s*exit\s+0\s*$"), "在失败分支 exit 0 判绿"),
    (re.compile(r"continue-on-error\s*:\s*true"), "continue-on-error 吞掉失败"),
    (re.compile(r"\|\|\s*true\b"), "`|| true` 吞掉失败"),
    (re.compile(r"::warning"), "把失败降级成 warning"),
    (re.compile(r"\bSKIP\b"), "SKIP 降级路径"),
)


def _false_green_escapes(text):
    """返回把非 0 退出洗成绿的证据行（行, 原因）；无则空列表。注释行不算。"""
    found = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        for pattern, why in _FALSE_GREEN:
            if pattern.search(line):
                found.append((raw, why))
                break
    return found


def _top_level_block(text, key):
    """取出顶格 `<key>:` 的缩进块（到下一个顶格键为止）；找不到返回空串。"""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.rstrip() == key + ":":
            out = []
            for follow in lines[i + 1:]:
                if follow.strip() and not follow.startswith((" ", "\t")):
                    break
                out.append(follow)
            return "\n".join(out)
    return ""


def _job_blocks(text):
    """返回 {job_id: block}：`jobs:` 下 2 空格缩进的每个 job 块。"""
    return {jid: _job_block(text, jid)
            for jid in re.findall(r"(?m)^  ([A-Za-z0-9_-]+):\s*$", text)}


class UpdateRollbackE2EWiringTest(unittest.TestCase):
    """U2 的干跑契约留在 required job；全矩阵只在手动自托管 workflow；都不许洗绿。"""

    def test_script_exists_and_parses(self):
        if not BASH:
            self.skipTest("bash 不在 PATH：更新演练脚本语法检查跳过（CI/WSL 有 bash）")
        self.assertTrue(os.path.isfile(UPDATE_ROLLBACK), "缺 %s" % UPDATE_ROLLBACK_REL)
        proc = subprocess.run([BASH, "-n", UPDATE_ROLLBACK],
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)
        self.assertEqual(proc.returncode, 0,
                         "bash -n 失败：%s\n%s" % (UPDATE_ROLLBACK_REL,
                                                  proc.stderr.decode("utf-8", "replace")))

    def test_dry_run_contract_stays_in_the_required_job(self):
        block = _job_block(_read(os.path.join(WORKFLOWS, "deploy-gate.yml")), DRY_RUN_JOB)
        self.assertTrue(block, "deploy-gate.yml 里没有 %s job（required 检查）" % DRY_RUN_JOB)
        self.assertIn("timeout-minutes:", block, "required job 没有超时门")
        # 没有 docker 的机器上也必须有一条真跑的命令：update.sh 干跑契约。
        self.assertIn("go test", block, "required job 不再跑 update.sh 干跑契约")
        self.assertIn("TestUpdateSh", block, "干跑命令没点名 TestUpdateSh")
        self.assertIn("TestInstallSh", block, "干跑命令没点名 TestInstallSh")
        self.assertIn("| tee /tmp/update-sh-e2e.json", block,
                      "go test 失败/跳过时日志必须留在 CI 输出里")
        self.assertEqual(_false_green_escapes(block), [],
                         "required job 出现把非 0 洗成绿的分支：任何失败都必须判红")

    def test_required_job_no_longer_runs_the_privileged_matrix(self):
        text = _read(os.path.join(WORKFLOWS, "deploy-gate.yml"))
        self.assertNotIn(UPDATE_ROLLBACK_REL, text,
                         "托管 runner 上跑不了特权 systemd：deploy-gate.yml 不得再引用演练脚本"
                         "（全矩阵改由自托管 workflow 承载）")

    def test_matrix_runs_in_exactly_one_manual_self_hosted_workflow(self):
        hits = _workflows_invoking(WORKFLOWS, _UPDATE_ROLLBACK_INVOCATION)
        self.assertEqual(len(hits), 1,
                         "全矩阵必须只由一条独立 workflow 承载：%s" % hits)
        name = hits[0]
        self.assertNotIn(name, ("deploy-gate.yml", "ci.yml"),
                         "全矩阵不得留在托管 runner 的 workflow 里（P1-6）")
        text = _read(os.path.join(WORKFLOWS, name))
        on_block = _on_block(text)
        self.assertIn("workflow_dispatch", on_block, "全矩阵 workflow 必须手动触发")
        self.assertFalse(_has_auto_trigger(text),
                         "全矩阵只在自托管 runner 上手动跑：不得挂 push/pull_request")
        job = next((b for b in _job_blocks(text).values() if UPDATE_ROLLBACK_REL in b), "")
        self.assertTrue(job, "全矩阵 workflow 里没有真的调用演练脚本的 job")
        self.assertIn("timeout-minutes:", job, "全矩阵 job 没有超时门")
        self.assertRegex(job, r"(?m)^\s+runs-on:.*self-hosted",
                         "全矩阵必须跑在自托管 runner（托管 runner 起不了特权 systemd）")
        self.assertIn("yiban-privileged", job,
                      "自托管 runner 标签必须显性登记（注册 runner 时用同名标签）")
        self.assertIn("update-rollback-e2e.sh host", job,
                      "必须走 host 驱动模式（脚本自己建一次性容器壳）")
        perms = _top_level_block(text, "permissions")
        self.assertIn("contents: read", perms, "全矩阵 workflow 必须声明最小权限")
        self.assertNotRegex(perms, r":\s*write", "全矩阵 workflow 不得申请写权限")
        self.assertEqual(_false_green_escapes(job), [],
                         "全矩阵 workflow 出现把非 0 洗成绿的分支（P1-6）")


class NonZeroToGreenCheckerTest(unittest.TestCase):
    """活体反例：检查器对旧 SKIP 分支必须报警，对 fail-closed 写法必须判净。"""

    OLD_SKIP_BLOCK = (
        "    steps:\n"
        "      - name: matrix\n"
        "        run: |\n"
        "          set +e\n"
        "          bash scripts/e2e/update-rollback-e2e.sh host --log-dir \"$RUNNER_TEMP/x\"\n"
        "          rc=$?\n"
        "          if [ \"$rc\" -eq 2 ]; then\n"
        "            echo \"SKIP: 环境起不来，演练跳过\"\n"
        "            echo \"::warning title=update-rollback-e2e SKIP::演练跳过\"\n"
        "            exit 0\n"
        "          fi\n"
        "          exit \"$rc\"\n")

    FAIL_CLOSED_BLOCK = (
        "    steps:\n"
        "      - name: matrix\n"
        "        run: |\n"
        "          bash scripts/e2e/update-rollback-e2e.sh host --log-dir \"$RUNNER_TEMP/x\"\n")

    def test_the_old_skip_escape_is_flagged(self):
        reasons = [why for _, why in _false_green_escapes(self.OLD_SKIP_BLOCK)]
        self.assertGreaterEqual(len(reasons), 4,
                                "旧 SKIP 分支必须被多条判据同时抓住，实际只抓到：%s" % reasons)

    def test_fail_closed_step_is_clean(self):
        self.assertEqual(_false_green_escapes(self.FAIL_CLOSED_BLOCK), [],
                         "非 0 直接冒泡的 fail-closed 写法不得被误判成逃逸")

    def test_comment_only_mentions_are_ignored(self):
        text = "      # exit 0 / -eq 2 / SKIP / continue-on-error: true 都是假绿\n"
        self.assertEqual(_false_green_escapes(text), [],
                         "注释里解释这些词不算逃逸（否则守卫无法写清口径）")

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
