# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""契约门（spectral ＋ oasdiff）的 CI 接线与入口脚本守卫。

# 功能

钉住七件事：

1. `ci.yml` 有 `contract-gate` 作业，声明超时，且没有作业级 `if`。
2. 作业真的执行 `bash scripts/contract-gate.sh`，并传入 `--base`
   （取自改动面分类的 scope 输出）。
3. 改动面判据只认 `server/contracts/`；非契约改动时作业显性早退，
   SKIP 行同时进 stdout 与 `$GITHUB_STEP_SUMMARY`。
4. 早退之后的重步（装 spectral、装 oasdiff、跑门）都挂 `!= 'skip'` 条件。
5. PR 事件拿不到底本时 fail-closed（保留 ref，入口脚本 exit 2）；push
   事件才显性 SKIP。
6. 工具钉版：spectral 6.17.0 在 `server/contracts/tools` 的清单与锁里；
   oasdiff 按 `v1.35.0` 安装；`npm ci` 带 `--ignore-scripts`（阻断
   postinstall 联网）。
7. 入口脚本的行为：非注释行真的跑 spectral（`--fail-severity warn`）与
   oasdiff（`--format json` ＋ allowlist 过滤）；缺工具 exit 2；无底本时
   打印显性 SKIP；破坏性变更的退出码原样传出；allowlist 过期行失效
   （过期/缺列都回到红）。

# 归属

测试/门禁面。

# 口径

文本断言与行为断言分开：`_violations` 是纯函数（主断言与突变自证共用），
`EntryScriptBehaviorTest` 用临时 stub 真跑入口脚本。
"""

import io
import json
import os
import re
import subprocess
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CI = os.path.join(BASE, ".github", "workflows", "ci.yml")
ENTRY = os.path.join(BASE, "scripts", "contract-gate.sh")
RULESET = os.path.join(BASE, "server", "contracts", ".spectral.yaml")
TOOLS_DIR = os.path.join(BASE, "server", "contracts", "tools")
PKG = os.path.join(TOOLS_DIR, "package.json")
LOCK = os.path.join(TOOLS_DIR, "package-lock.json")

JOB = "contract-gate"
SKIP_PREFIX = "contract-gate SKIP (scope="
ENTRY_CMD = "bash scripts/contract-gate.sh"
SPECTRAL_PIN = "6.17.0"
OASDIFF_PIN = "oasdiff@v1.35.0"
NPM_INSTALL = "npm ci --prefix server/contracts/tools"


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
    out = [lines[start]]
    for line in lines[start + 1:]:
        if re.match(r"^  [A-Za-z0-9_-]+:\s*$", line):
            break
        out.append(line)
    return "\n".join(out)


_STEP_HEAD = re.compile(r"^      - ")
_ATTR = re.compile(r"^        \S")
_BLOCK_SCALAR = ("|", "|-", ">", ">-")


def _steps(block):
    """把 job block 切成步骤：`[{"name", "if", "run"}, ...]`。

    `run` 是命令行列表：内联 `run:` 取一行；块式取之后的缩进行。
    注释行剥掉（注释不算调用）。`uses:`/`with:` 不进 `run`。
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
                in_block = not cur["run"]
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
                in_block = not cur["run"]
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
    """真调用判据：某条未注释命令行**以** cmd 开头（cmd 后是行尾或空白）。"""
    return any(line == cmd or line.startswith(cmd + " ") for line in step["run"])


def _violations(block):
    """纯函数：给 `contract-gate` 的 job block 文本，返回违规清单（空 = 合规）。"""
    bad = []
    if not block:
        bad.append("作业 %s 不存在：契约门没有 CI 调用点" % JOB)
        return bad
    steps = _steps(block)
    if "timeout-minutes:" not in block:
        bad.append("%s 没有超时门（timeout-minutes）" % JOB)
    for line in block.splitlines():
        if re.match(r"^    if:", line):
            bad.append("%s 出现作业级 if（%r）：为假时作业不被调度，SKIP 行没有打印机会"
                       % (JOB, line.strip()))

    gate_idx = [i for i, s in enumerate(steps) if _invokes(s, ENTRY_CMD)]
    if not gate_idx:
        bad.append("入口脚本 %r 没有被真调用（注释或 echo 不算）" % ENTRY_CMD)
    else:
        # 只调脚本不够：不传 --base 会让 oasdiff 半边永远走 SKIP，作业照样绿。
        # 只认调用行本身；同一 run 块里的 echo 带字样不算。
        gate_lines = [ln for i in gate_idx for ln in steps[i]["run"]
                      if ln == ENTRY_CMD or ln.startswith(ENTRY_CMD + " ")]
        if not any("--base" in ln and "steps.scope.outputs.base" in ln for ln in gate_lines):
            bad.append("入口脚本没有收到 `--base`（及其 scope 输出）：oasdiff 半边会被永远 SKIP")

    skip_steps = [s for s in steps if any(SKIP_PREFIX in ln for ln in s["run"])]
    if not skip_steps:
        bad.append("非契约改动没有显性 SKIP 输出行 %r（跳过必须可见）" % SKIP_PREFIX)
    else:
        if not any("== 'skip'" in s["if"] for s in skip_steps):
            bad.append("SKIP 行所在步骤没有 `== 'skip'` 条件：无关 PR 上照打印，早退是假的")
        if not any("$GITHUB_STEP_SUMMARY" in ln for s in skip_steps for ln in s["run"]):
            bad.append("SKIP 行只进 stdout 不进 $GITHUB_STEP_SUMMARY：聚合页看不见")

    # PR 事件拿不到底本时必须 fail-closed（保留 ref 交给入口脚本 exit 2）。
    if not any('base="origin/${{ github.base_ref }}"' in ln
               for s in steps for ln in s["run"]):
        bad.append("PR 事件底本取不到时没有 fail-closed 分支：契约 PR 会降级为绿")
    elif not any('elif [ -n "${{ github.base_ref }}" ]; then' in ln
                 for s in steps for ln in s["run"]):
        bad.append("fail-closed 分支没有挂在 base_ref 非空的条件上：push 事件也会变红")

    scope_hit = any("server/contracts/" in ln and "grep -E" in ln
                    for s in steps for ln in s["run"])
    if not scope_hit:
        bad.append("改动面判据没有按 server/contracts/ 过滤：契约门会在无关 PR 上真跑或错误跳过")

    install_hit = any(_invokes(s, NPM_INSTALL) for s in steps)
    if not install_hit:
        bad.append("没有按锁安装契约工具（%r）" % NPM_INSTALL)
    elif not any("--ignore-scripts" in ln
                 for s in steps if _invokes(s, NPM_INSTALL)
                 for ln in s["run"]):
        bad.append("npm ci 缺 --ignore-scripts：postinstall 会发网络请求")

    if not any(OASDIFF_PIN in ln for s in steps for ln in s["run"]):
        bad.append("oasdiff 没有按 %s 安装（门禁工具必须钉版）" % OASDIFF_PIN)

    for s in steps:
        touches = (_invokes(s, ENTRY_CMD)
                   or any(ln.startswith(NPM_INSTALL) for ln in s["run"])
                   or any(OASDIFF_PIN in ln for ln in s["run"]))
        if touches and "!= 'skip'" not in s["if"]:
            bad.append("步骤 %r 没挂 `!= 'skip'` 条件：SKIP 行打了它照跑，早退是假的" % s["name"])
    return bad


class ContractGateWiringTest(unittest.TestCase):
    """主断言：真树上的 `contract-gate` 作业满足全部接线不变量。"""

    def test_job_is_fully_wired(self):
        bad = _violations(_job_block(_read(CI), JOB))
        self.assertEqual(
            bad, [],
            "ci.yml 的 %s 作业接线不合规：\n  - " % JOB + "\n  - ".join(bad))


class WiringCheckerMutationTest(unittest.TestCase):
    """检查函数自身的突变自证：合成输入必须判红（防恒真）。"""

    GOOD_STEPS = (
        "    timeout-minutes: 10\n"
        "    steps:\n"
        "      - name: Classify change surface\n"
        "        run: |\n"
        "          changed=$(git diff --name-only base HEAD)\n"
        "          printf '%s\\n' \"$changed\" | grep -E '^server/contracts/' >/dev/null\n"
        "          base_ok=0\n"
        "          if git rev-parse --verify \"$base\" >/dev/null 2>&1; then\n"
        "            base_ok=1\n"
        "          elif [ -n \"${{ github.base_ref }}\" ]; then\n"
        "            base=\"origin/${{ github.base_ref }}\"\n"
        "          else\n"
        "            base=\"\"\n"
        "          fi\n"
        "      - name: Skip (out of change surface)\n"
        "        if: steps.scope.outputs.value == 'skip'\n"
        "        run: |\n"
        "          line=\"contract-gate SKIP (scope=no-contract-changes)\"\n"
        "          echo \"$line\"\n"
        "          echo \"$line\" >> \"$GITHUB_STEP_SUMMARY\"\n"
        "      - name: Install pinned spectral\n"
        "        if: steps.scope.outputs.value != 'skip'\n"
        "        run: npm ci --prefix server/contracts/tools --ignore-scripts --no-audit --no-fund\n"
        "      - name: Install pinned oasdiff\n"
        "        if: steps.scope.outputs.value != 'skip'\n"
        "        run: go install github.com/oasdiff/oasdiff@v1.35.0\n"
        "      - name: Contract gate\n"
        "        if: steps.scope.outputs.value != 'skip'\n"
        "        run: |\n"
        "          echo \"--base ${{ steps.scope.outputs.base }}\"\n"
        "          bash scripts/contract-gate.sh --base \"${{ steps.scope.outputs.base }}\"\n"
    )

    def test_good_block_is_green(self):
        self.assertEqual(_violations(self.GOOD_STEPS), [],
                         "合成合规样本被误判红：判据过严，主断言会失去意义")

    def test_missing_job_is_red(self):
        bad = _violations("")
        self.assertEqual(len(bad), 1, "空 block 应只报作业缺失：%r" % bad)
        self.assertIn("不存在", bad[0])

    def test_echo_only_command_is_red(self):
        block = self.GOOD_STEPS.replace(
            '          bash scripts/contract-gate.sh --base "${{ steps.scope.outputs.base }}"',
            '          echo "bash scripts/contract-gate.sh"')
        bad = _violations(block)
        self.assertTrue(any("没有被真调用" in b for b in bad), bad)

    def test_missing_base_argument_is_red(self):
        block = self.GOOD_STEPS.replace(
            ' --base "${{ steps.scope.outputs.base }}"', "")
        bad = _violations(block)
        self.assertTrue(any("--base" in b for b in bad),
                        "删掉 --base 后 oasdiff 半边永远 SKIP，必须判红：%r" % bad)

    def test_echo_of_base_argument_is_red(self):
        # 调用行丢了 --base、同一 run 块里的 echo 还带着字样：必须判红。
        block = self.GOOD_STEPS.replace(
            '          bash scripts/contract-gate.sh --base "${{ steps.scope.outputs.base }}"',
            "          bash scripts/contract-gate.sh")
        bad = _violations(block)
        self.assertTrue(any("--base" in b for b in bad),
                        "echo 带 --base 字样不算收到参数：%r" % bad)

    def test_failclosed_without_base_ref_condition_is_red(self):
        block = self.GOOD_STEPS.replace(
            'elif [ -n "${{ github.base_ref }}" ]; then', "elif true; then")
        bad = _violations(block)
        self.assertTrue(any("push 事件" in b for b in bad), bad)

    def test_job_level_if_is_red(self):
        bad = _violations(self.GOOD_STEPS.replace(
            "    timeout-minutes: 10\n", "    if: github.event_name == 'pull_request'\n"))
        self.assertTrue(any("作业级 if" in b for b in bad), bad)

    def test_skip_line_without_summary_is_red(self):
        block = self.GOOD_STEPS.replace(
            '          echo "$line" >> "$GITHUB_STEP_SUMMARY"\n', "")
        bad = _violations(block)
        self.assertTrue(any("GITHUB_STEP_SUMMARY" in b for b in bad), bad)

    def test_skip_step_without_condition_is_red(self):
        block = self.GOOD_STEPS.replace(
            "      - name: Skip (out of change surface)\n"
            "        if: steps.scope.outputs.value == 'skip'\n",
            "      - name: Skip (out of change surface)\n")
        bad = _violations(block)
        self.assertTrue(any("早退是假的" in b for b in bad), bad)

    def test_missing_failclosed_branch_is_red(self):
        block = self.GOOD_STEPS.replace('          base="origin/${{ github.base_ref }}"\n', "")
        bad = _violations(block)
        self.assertTrue(any("fail-closed" in b for b in bad), bad)

    def test_gate_step_without_skip_condition_is_red(self):
        block = self.GOOD_STEPS.replace(
            "      - name: Contract gate\n"
            "        if: steps.scope.outputs.value != 'skip'\n",
            "      - name: Contract gate\n")
        bad = _violations(block)
        self.assertTrue(any("早退是假的" in b for b in bad), bad)

    def test_wrong_scope_filter_is_red(self):
        block = self.GOOD_STEPS.replace("^server/contracts/", "^ui/")
        bad = _violations(block)
        self.assertTrue(any("改动面判据" in b for b in bad), bad)


class EntryScriptTest(unittest.TestCase):
    """入口脚本的非注释行必须真跑两个工具，且环境错误 exit 2。"""

    @classmethod
    def setUpClass(cls):
        cls.script = _read(ENTRY)

    def _code_lines(self):
        return [ln for ln in self.script.splitlines() if not ln.strip().startswith("#")]

    def test_runs_spectral_with_warn_severity(self):
        hits = [ln for ln in self._code_lines()
                if "spectral" in ln.lower() and "--fail-severity warn" in ln]
        self.assertTrue(hits, "入口脚本没有非注释的 spectral 调用（--fail-severity warn）")

    def test_runs_oasdiff_breaking_through_allowlist_filter(self):
        # 测试改动理由（规则 10；kqa 收口 2026-11-10 独立复审同步）：门禁 oasdiff
        # 半边从 `--fail-on ERR` 改为 `--format json`＋仓内 filter（allowlist 逐条
        # 豁免、只 ERR 级判红、缺解释器 exit 2）。断言随新语义改钉：非注释调用行、
        # 默认名册路径、fail-closed 解释器判据。
        code = self._code_lines()
        hits = [ln for ln in code
                if "breaking" in ln and "--format json" in ln and "oasdiff" in ln.lower()]
        self.assertTrue(hits, "入口脚本没有非注释的 oasdiff breaking --format json 调用")
        joined = "\n".join(code)
        self.assertIn(".breaking-allowlist.tsv", joined,
                      "豁免名册默认路径没进脚本非注释行")
        self.assertIn("python3 not found", joined,
                      "缺 python 解释器的 fail-closed（exit 2）路径没被钉住")

    def test_missing_tool_exits_2(self):
        self.assertIn("exit 2", self.script,
                      "缺工具必须 exit 2：跳过不得当绿")
        self.assertRegex(self.script, r"die_env\(\) \{",
                         "缺工具路径要走统一的 die_env")

    def test_no_base_prints_visible_skip(self):
        self.assertIn("contract-gate SKIP (oasdiff:", self.script,
                      "没有底本时必须打印显性 SKIP 行")

    def test_base_without_spec_is_an_environment_error(self):
        self.assertIn("does not exist at base ref", self.script,
                      "给了 --base 但取不到 spec 必须 exit 2，不得静默跳过")


def _ruleset_violations(body):
    """纯函数：给规则集文本，返回违规清单（空 = 合规）。"""
    bad = []
    if not re.search(r"(?m)^\s*- spectral:oas\s*$", body):
        bad.append("规则集必须继承 spectral:oas")
    # 关闭规则的两种写法都要拦：裸 `off` 行与 `rule: off|0|false`。
    if re.search(r"(?m)^\s*(off|0|false)\s*$", body) or re.search(
            r"(?mi)^\s*-?\s*[a-z0-9_-]+:\s*(off|0|false)\s*$", body):
        bad.append("规则集不得逐条关闭（关闭即静默）")
    return bad


class RulesetAndPinsTest(unittest.TestCase):
    """规则集与工具版本的钉法。"""

    def test_ruleset_extends_canonical_oas_set(self):
        bad = _ruleset_violations(_read(RULESET))
        self.assertEqual(bad, [], "规则集不合规：%r" % bad)

    def test_disabled_rule_is_red(self):
        mutated = "extends:\n  - spectral:oas\nrules:\n  operation-description: off\n"
        self.assertTrue(any("不得逐条关闭" in b for b in _ruleset_violations(mutated)),
                        "关闭规则必须判红：%r" % _ruleset_violations(mutated))

    def test_missing_extends_is_red(self):
        self.assertTrue(any("必须继承" in b for b in _ruleset_violations("rules: {}\n")),
                        "缺 extends 必须判红")

    def test_spectral_is_pinned_in_manifest_and_lock(self):
        pkg = json.loads(_read(PKG))
        dev = pkg.get("devDependencies", {})
        self.assertEqual(dev.get("@stoplight/spectral-cli"), SPECTRAL_PIN,
                         "spectral 必须按精确版本钉在 tools/package.json")
        lock = json.loads(_read(LOCK))
        entry = lock.get("packages", {}).get("node_modules/@stoplight/spectral-cli", {})
        self.assertEqual(entry.get("version"), SPECTRAL_PIN,
                         "锁文件里的 spectral 版本与清单不一致")

    def test_entry_script_is_tracked(self):
        self.assertTrue(os.path.isfile(ENTRY), "scripts/contract-gate.sh 不存在")


def _write_stub(path, body):
    """写一个可执行的 bash stub（Windows 下 chmod 由 MSYS 解释）。"""
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(body)
    os.chmod(path, 0o755)


class EntryScriptBehaviorTest(unittest.TestCase):
    """真跑入口脚本：stub 掉两个工具，断言退出码与 SKIP 行。

    为什么需要这一组：EntryScriptTest 只读文本；脚本在 `set -euo pipefail`
    之后插一句 `exit 0`，那 5 条断言仍全绿。这里用临时 stub 跑真脚本，
    钉住「无底本退出 0 且打印 SKIP」「破坏性变更退出 1」「缺工具退出 2」。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # MSYS bash 接受 C:/... 形式；Windows 反斜杠路径在 exec 时会失配。
        tmp = self.tmp.name.replace("\\", "/")
        self.spectral = tmp + "/spectral"
        self.oasdiff = tmp + "/oasdiff"
        _write_stub(self.spectral, "#!/usr/bin/env bash\necho spectral-stub\n")
        # stub 输出合法 JSON 的一条 level=1（非 ERR）finding：门机制改走
        # --format json＋仓内 filter 后，stub 必须产可解析结构（kqa 收口 2026-11-10）。
        _write_stub(self.oasdiff,
                    "#!/usr/bin/env bash\n"
                    "echo '[{\"id\":\"stub-check\",\"operation\":\"GET\",\"path\":\"/stub\","
                    "\"level\":1,\"text\":\"oasdiff-stub-note\"}]'\n"
                    "exit \"${STUB_OASDIFF_RC:-0}\"\n")

    def _run(self, *args, **env):
        run_env = dict(os.environ)
        # 不继承 CI 的 step summary 文件：否则本测试会给 verify 作业的
        # summary 写一条假的 contract-gate SKIP 行。
        run_env.pop("GITHUB_STEP_SUMMARY", None)
        run_env.update(SPECTRAL=self.spectral, OASDIFF=self.oasdiff)
        run_env.update(env)
        proc = subprocess.run(["bash", ENTRY, *args], cwd=BASE, env=run_env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=120)
        return proc.returncode, proc.stdout

    def test_no_base_skips_visibly_and_passes(self):
        rc, out = self._run()
        self.assertEqual(rc, 0, out)
        self.assertIn("contract-gate SKIP (oasdiff:", out)

    def test_skip_line_reaches_step_summary(self):
        summary = os.path.join(self.tmp.name, "summary.md")
        rc, out = self._run(GITHUB_STEP_SUMMARY=summary)
        self.assertEqual(rc, 0, out)
        with io.open(summary, encoding="utf-8") as f:
            self.assertIn("contract-gate SKIP (oasdiff:", f.read())

    def test_base_runs_oasdiff(self):
        rc, out = self._run("--base", "HEAD")
        self.assertEqual(rc, 0, out)
        self.assertIn("== oasdiff breaking", out)
        # stub 的 level=1 finding 必须被 filter 解析并展示（NOTE 行含 text），
        # 非 ERR 不判红——同时证明 stub 真被调用、JSON 半边真在链上。
        self.assertIn("oasdiff-stub-note", out)
        self.assertIn("oasdiff breaking: ok", out)

    def test_breaking_exit_is_propagated(self):
        rc, out = self._run("--base", "HEAD", STUB_OASDIFF_RC="1")
        self.assertEqual(rc, 1, out)

    def _write_allowlist(self, *rows):
        """写临时豁免名册（每行一个字符串），返回 MSYS 友好路径。"""
        path = os.path.join(self.tmp.name, "allow.tsv")
        with io.open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write("# test allowlist\n")
            for row in rows:
                f.write(row + "\n")
        return path.replace("\\", "/")

    def _write_err_oasdiff(self):
        """写一个产出 ERR 级 finding 的 oasdiff stub。"""
        path = self.tmp.name.replace("\\", "/") + "/oasdiff-err"
        _write_stub(path,
                    "#!/usr/bin/env bash\n"
                    "echo '[{\"id\":\"stub-check\",\"operation\":\"GET\",\"path\":\"/stub\","
                    "\"level\":3,\"text\":\"breaking-stub\"}]'\n")
        return path

    def test_expired_allowlist_row_does_not_exempt(self):
        # 过期行必须失效：finding 回到红，进程非零。
        allow = self._write_allowlist(
            "stub-check\tget\t/stub\tbatch\t2000-01-01")
        rc, out = self._run("--base", "HEAD",
                            BREAKS_ALLOWLIST=allow,
                            OASDIFF=self._write_err_oasdiff())
        self.assertEqual(rc, 1, out)
        self.assertIn("EXPIRED", out)
        self.assertIn("breaking-stub", out)

    def test_future_allowlist_row_still_exempts(self):
        # 正向对照：未过期行仍放行，证明过期判据不是恒红。
        allow = self._write_allowlist(
            "stub-check\tget\t/stub\tbatch\t2999-12-31")
        rc, out = self._run("--base", "HEAD",
                            BREAKS_ALLOWLIST=allow,
                            OASDIFF=self._write_err_oasdiff())
        self.assertEqual(rc, 0, out)
        self.assertIn("ALLOWED", out)

    def test_allowlist_row_without_expiry_is_invalid(self):
        # 缺过期列按失效处理：新增行不能绕过到期评审。
        allow = self._write_allowlist("stub-check\tget\t/stub\tbatch")
        rc, out = self._run("--base", "HEAD",
                            BREAKS_ALLOWLIST=allow,
                            OASDIFF=self._write_err_oasdiff())
        self.assertEqual(rc, 1, out)
        self.assertIn("INVALID", out)

    def test_missing_spectral_is_an_environment_error(self):
        rc, out = self._run(SPECTRAL=self.tmp.name.replace("\\", "/") + "/absent")
        self.assertEqual(rc, 2, out)
        self.assertIn("spectral not found", out)

    def test_missing_oasdiff_is_an_environment_error(self):
        rc, out = self._run("--base", "HEAD",
                            OASDIFF=self.tmp.name.replace("\\", "/") + "/absent")
        self.assertEqual(rc, 2, out)
        self.assertIn("oasdiff not found", out)

    def test_unresolvable_base_is_an_environment_error(self):
        rc, out = self._run("--base", "deadbeefdeadbeef")
        self.assertEqual(rc, 2, out)
        self.assertIn("not resolvable", out)


if __name__ == "__main__":
    unittest.main()
