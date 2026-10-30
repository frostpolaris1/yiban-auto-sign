# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""新线 UI Playwright e2e 的 CI 接线守卫（工单 9f6 / E-1，2026-10-31）。

标签：J · 运维：CI 接线
覆盖：AGENTS.md §30（跳过 ≠ 绿，跳过必须显性可见）与 §十三.29（门看的东西＝实际
    改的东西）。`ui/e2e` 的 Playwright 套件（两条分片：行为 `test:behavior`、
    视觉 `test:shots`）此前只在本机可跑；本文件钉住 `.github/workflows/ci.yml`
    的 `ui-e2e` 作业把套件真接进自动轨。
本文件钉四条不变量：
  ① `ci.yml` 有 `ui-e2e` 作业（找不到即红：扫空不得当绿），且声明超时门；
  ② 两个分片命令被**真调用**——只认未注释 `run:` 行里以命令本体开头的行；
     注释或 `echo` 里出现不算（判据形状沿 `tests/test_deploy_e2e_wiring.py`
     的「echo/注释不算调用」）；
  ③ `npm run build` 以真 run 步骤出现在两个分片命令**之前**
     （`ui/playwright.config.ts` 的 4173 webServer 只伺服 `dist`，不自动构建）；
  ④ 非 ui 改动时作业仍被调度并**显性早退**：run 文本打印
     `ui-e2e SKIP (scope=`、同一 run 文本写 `$GITHUB_STEP_SUMMARY`，
     且分片步骤与构建步骤必须挂在 `!= 'skip'` 条件下——没有条件＝SKIP 行
     打了测试照跑，早退是假的。
  另钉：不许作业级 `if`（为假时作业不被调度，SKIP 行没有打印机会，
  required-check 语义有歧义）；步骤级判定不受此限。
活体反例（防恒真）：作业缺失、分片命令换 echo、命令全注释、build 挪到分片之后、
    SKIP 行被删、SKIP 不进 step summary、分片步骤不挂 skip 条件、加作业级 if，
    各自必须判红。
判据实现：`_violations()` 是纯函数，主断言与突变自证共用同一份判据。
依赖：只读文件文本；不跑子进程、不触网。整文件无 skip。
"""
import io
import os
import re
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CI = os.path.join(BASE, ".github", "workflows", "ci.yml")
JOB = "ui-e2e"
BUILD_CMD = "npm run build"
SHARD_COMMANDS = ("npm run test:behavior", "npm run test:shots")
SKIP_PREFIX = "ui-e2e SKIP (scope="


def _read(path):
    with io.open(path, encoding="utf-8") as f:
        return f.read()


def _job_block(text, job):
    """取出 `  <job>:` 到下一个 2 空格顶层键之间的文本；找不到返回空串。"""
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


_STEP_HEAD = re.compile(r"^      - ")
_ATTR = re.compile(r"^        \S")
_BLOCK_SCALAR = ("|", "|-", ">", ">-")


def _steps(block):
    """把 job block 切成步骤：`[{"name", "if", "run"}, ...]。

    `run` 是该步骤的命令行列表：内联 `run: <cmd>` 取一行；块式 `run: |` 取
    之后的缩进行。注释行剥掉（注释不算调用）。`uses:`/`with:` 不进 `run`。
    """
    steps = []
    cur = None
    in_block = False
    for raw in block.splitlines():
        if _STEP_HEAD.match(raw):
            if cur is not None:
                steps.append(cur)
            cur = {"name": "", "if": "", "run": []}
            in_block = False
            rest = raw.rstrip()[len("      - "):].strip()
            if rest.startswith("name:"):
                cur["name"] = rest[len("name:"):].strip()
            elif rest.startswith("run:"):
                _take_run(cur, rest[len("run:"):].strip())
                in_block = cur["run"] == []
            continue
        if cur is None:
            continue
        if _ATTR.match(raw):
            in_block = False
            s = raw.strip()
            if s.startswith("name:"):
                cur["name"] = s[len("name:"):].strip()
            elif s.startswith("if:"):
                cur["if"] = s[len("if:"):].strip()
            elif s.startswith("run:"):
                _take_run(cur, s[len("run:"):].strip())
                in_block = cur["run"] == []
            continue
        if in_block:
            s = raw.strip()
            if s and not s.startswith("#"):
                cur["run"].append(s)
    if cur is not None:
        steps.append(cur)
    return steps


def _take_run(cur, rest):
    """记录一条内联 run 命令；块式标量（`|`）留给后续缩进行。"""
    if rest and rest not in _BLOCK_SCALAR:
        cur["run"].append(rest)


def _invokes(step, cmd):
    """真调用判据：某条未注释命令行**以** cmd 开头（cmd 后是行尾或空白）。

    `echo "npm run test:behavior"` 的命令行以 `echo` 开头，不算；
    `# run: npm run …` 在提取阶段已剥掉，也不算。
    """
    return any(line == cmd or line.startswith(cmd + " ") for line in step["run"])


def _violations(block):
    """纯函数：给 `ui-e2e` 的 job block 文本，返回违规清单（空 = 合规）。

    单独成纯函数是为了能被**突变自证**调用：喂合成输入必须判出对应违规。
    """
    bad = []
    if not block:
        bad.append("作业 %s 不存在：ui/e2e 的 Playwright 套件没有 CI 调用点" % JOB)
        return bad
    steps = _steps(block)
    if "timeout-minutes:" not in block:
        bad.append("%s 没有超时门（timeout-minutes）" % JOB)
    shard_idx = {}
    for cmd in SHARD_COMMANDS:
        idx = [i for i, s in enumerate(steps) if _invokes(s, cmd)]
        if not idx:
            bad.append("分片命令 %s 没有被真调用（注释或 echo 不算）" % cmd)
        else:
            shard_idx[cmd] = min(idx)
    build_idx = [i for i, s in enumerate(steps) if _invokes(s, BUILD_CMD)]
    if not build_idx:
        bad.append("%s 没有被真调用（4173 webServer 只伺服 dist，不自动构建）" % BUILD_CMD)
    else:
        first_build = min(build_idx)
        for cmd, i in sorted(shard_idx.items()):
            if first_build > i:
                bad.append("%s（步骤序号 %d）必须先于分片命令 %s（步骤序号 %d）"
                           % (BUILD_CMD, first_build, cmd, i))
    skip_steps = [s for s in steps if any(SKIP_PREFIX in ln for ln in s["run"])]
    if not skip_steps:
        bad.append("非 ui 改动没有显性 SKIP 输出行 %r（AGENTS §30：跳过 ≠ 绿）" % SKIP_PREFIX)
    elif not any("$GITHUB_STEP_SUMMARY" in ln for s in skip_steps for ln in s["run"]):
        bad.append("SKIP 行只进 stdout 不进 $GITHUB_STEP_SUMMARY：聚合页看不见")
    for cmd in (BUILD_CMD, *SHARD_COMMANDS):
        for s in steps:
            if _invokes(s, cmd) and "!= 'skip'" not in s["if"]:
                bad.append("步骤 %r（%s）没挂 `!= 'skip'` 条件：SKIP 行打了它照跑，早退是假的"
                           % (s["name"], cmd))
    for line in block.splitlines():
        if re.match(r"^    if:", line):
            bad.append("%s 出现作业级 if（%r）：为假时作业不被调度，SKIP 行没有打印机会"
                       % (JOB, line.strip()))
    return bad


class UiE2eJobWiringTest(unittest.TestCase):
    """① 主断言：真树上的 `ui-e2e` 作业满足全部四条不变量。"""

    def test_ui_e2e_job_is_fully_wired(self):
        block = _job_block(_read(CI), JOB)
        bad = _violations(block)
        self.assertEqual(
            bad, [],
            "ci.yml 的 %s 作业接线不合规：\n  - " % JOB + "\n  - ".join(bad))


class UiE2eWiringCheckerMutationTest(unittest.TestCase):
    """② 检查函数自身的突变自证：合成输入必须判红（防恒真）。

    为什么要有这一组：主断言若因某种原因恒真（解析器吞掉一切、判据写反），
    这道门就是摆设。每个突变喂一条**必须判红**的合成 job block。
    """

    def _bad_kinds(self, block):
        return _violations(block)

    def test_missing_job_is_red(self):
        bad = self._bad_kinds("")
        self.assertEqual(len(bad), 1, "空 block 应只报作业缺失：%r" % bad)
        self.assertIn("不存在", bad[0])

    def test_echo_only_shard_command_is_red(self):
        block = (
            "    timeout-minutes: 20\n"
            "    steps:\n"
            "      - name: Build UI\n"
            "        if: steps.scope.outputs.value != 'skip'\n"
            "        run: npm run build\n"
            "      - name: fake shard\n"
            "        if: steps.scope.outputs.value != 'skip'\n"
            "        run: echo \"npm run test:behavior\"\n"
            "        run: echo \"npm run test:shots\"\n"
            "      - name: Skip\n"
            "        if: steps.scope.outputs.value == 'skip'\n"
            "        run: |\n"
            "          line=\"ui-e2e SKIP (scope=x)\"\n"
            "          echo \"$line\"\n"
            "          echo \"$line\" >> \"$GITHUB_STEP_SUMMARY\"\n"
        )
        bad = self._bad_kinds(block)
        self.assertTrue(any("没有被真调用" in b and "test:behavior" in b for b in bad),
                         "echo 不算调用，必须判红：%r" % bad)

    def test_commented_out_command_is_red(self):
        block = (
            "    timeout-minutes: 20\n"
            "    steps:\n"
            "      - name: Build UI\n"
            "        if: steps.scope.outputs.value != 'skip'\n"
            "        run: npm run build\n"
            "      - name: fake shard\n"
            "        if: steps.scope.outputs.value != 'skip'\n"
            "        # run: npm run test:behavior\n"
            "        # run: npm run test:shots\n"
            "      - name: Skip\n"
            "        if: steps.scope.outputs.value == 'skip'\n"
            "        run: |\n"
            "          line=\"ui-e2e SKIP (scope=x)\"\n"
            "          echo \"$line\"\n"
            "          echo \"$line\" >> \"$GITHUB_STEP_SUMMARY\"\n"
        )
        bad = self._bad_kinds(block)
        self.assertTrue(any("没有被真调用" in b for b in bad),
                         "注释不算调用，必须判红：%r" % bad)

    def test_build_after_shards_is_red(self):
        block = (
            "    timeout-minutes: 20\n"
            "    steps:\n"
            "      - name: E2E shard\n"
            "        if: steps.scope.outputs.value != 'skip'\n"
            "        run: npm run test:behavior\n"
            "      - name: Build UI\n"
            "        if: steps.scope.outputs.value != 'skip'\n"
            "        run: npm run build\n"
            "      - name: Skip\n"
            "        if: steps.scope.outputs.value == 'skip'\n"
            "        run: |\n"
            "          line=\"ui-e2e SKIP (scope=x)\"\n"
            "          echo \"$line\"\n"
            "          echo \"$line\" >> \"$GITHUB_STEP_SUMMARY\"\n"
        )
        bad = self._bad_kinds(block)
        self.assertTrue(any("先于分片命令" in b for b in bad),
                         "build 在分片之后必须判红：%r" % bad)

    def test_missing_skip_line_is_red(self):
        block = (
            "    timeout-minutes: 20\n"
            "    steps:\n"
            "      - name: Build UI\n"
            "        if: steps.scope.outputs.value != 'skip'\n"
            "        run: npm run build\n"
            "      - name: E2E shard\n"
            "        if: steps.scope.outputs.value != 'skip'\n"
            "        run: npm run test:behavior\n"
        )
        bad = self._bad_kinds(block)
        self.assertTrue(any("SKIP" in b for b in bad),
                         "删掉 SKIP 行必须判红：%r" % bad)

    def test_skip_without_step_summary_is_red(self):
        block = (
            "    timeout-minutes: 20\n"
            "    steps:\n"
            "      - name: Build UI\n"
            "        if: steps.scope.outputs.value != 'skip'\n"
            "        run: npm run build\n"
            "      - name: E2E shard\n"
            "        if: steps.scope.outputs.value != 'skip'\n"
            "        run: npm run test:behavior\n"
            "      - name: Skip\n"
            "        if: steps.scope.outputs.value == 'skip'\n"
            "        run: echo \"ui-e2e SKIP (scope=x)\"\n"
        )
        bad = self._bad_kinds(block)
        self.assertTrue(any("GITHUB_STEP_SUMMARY" in b for b in bad),
                         "SKIP 不进 step summary 必须判红：%r" % bad)

    def test_ungated_shard_step_is_red(self):
        block = (
            "    timeout-minutes: 20\n"
            "    steps:\n"
            "      - name: Build UI\n"
            "        if: steps.scope.outputs.value != 'skip'\n"
            "        run: npm run build\n"
            "      - name: E2E shard\n"
            "        run: npm run test:behavior\n"
            "      - name: Skip\n"
            "        if: steps.scope.outputs.value == 'skip'\n"
            "        run: |\n"
            "          line=\"ui-e2e SKIP (scope=x)\"\n"
            "          echo \"$line\"\n"
            "          echo \"$line\" >> \"$GITHUB_STEP_SUMMARY\"\n"
        )
        bad = self._bad_kinds(block)
        self.assertTrue(any("没挂" in b and "test:behavior" in b for b in bad),
                         "分片步骤不挂 skip 条件必须判红：%r" % bad)

    def test_job_level_if_is_red(self):
        block = (
            "    if: github.ref == 'refs/heads/develop'\n"
            "    timeout-minutes: 20\n"
            "    steps:\n"
            "      - name: Build UI\n"
            "        if: steps.scope.outputs.value != 'skip'\n"
            "        run: npm run build\n"
            "      - name: E2E shard\n"
            "        if: steps.scope.outputs.value != 'skip'\n"
            "        run: npm run test:behavior\n"
            "      - name: Skip\n"
            "        if: steps.scope.outputs.value == 'skip'\n"
            "        run: |\n"
            "          line=\"ui-e2e SKIP (scope=x)\"\n"
            "          echo \"$line\"\n"
            "          echo \"$line\" >> \"$GITHUB_STEP_SUMMARY\"\n"
        )
        bad = self._bad_kinds(block)
        self.assertTrue(any("作业级 if" in b for b in bad),
                         "作业级 if 必须判红：%r" % bad)


if __name__ == "__main__":
    unittest.main()
