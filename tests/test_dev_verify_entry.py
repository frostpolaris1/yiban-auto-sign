# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""统一跑测入口 `scripts/dev-verify.sh` 的契约：跑法与 CI 关键子集命令不许漂移。

标签：J · 运维：部署/备份/发布
覆盖：本批把"跑测跑法"收成唯一入口——本地全量与 CI 关键子集同源，命令原文只存
    在入口脚本里一份（配方见 docs/dev/dev-verify.md）。四条不变量：
    ① **CI 关键子集命令逐字冻结**：从入口脚本的 `run_ci` 里按行抽出命令行，与
       下面的期望清单**逐字比对（含顺序）**。改一个参数（`-n 4`、`-k` 词集、
       `-p no:randomly`、ruff 扫描面、e2e smoke 目标）都必须同时改这里并在 PR 里
       写理由。这不是"第二份事实源"，是**冻结的期望**——本批硬约束是"CI 结果与
       改前等价"，此断言让漂移必须显式发生。
    ①′ 抽出"执行行"的口径认三种调用形态：`"$py" -m 模块`、`bash 脚本`、
       `"$py" 脚本路径`（B1 的 AST 门与 B2 的名册门都用第三种）。认少一种，那一步
       就不进冻结清单，命令原文就此多出第二份事实源。
    ② **全量参数固定**：默认模式 `-n auto --dist loadfile`。`--dist loadfile`
       不得去掉：套内存在文件内先后依赖与进程级 DB 单例，按单条分发即误红。
    ③ **固定 venv**：全量模式只认 `/root/.venv-yiban-wsl/bin/python`，不可用就
       响亮失败，不许退化成 PATH 上的任意 python。
    ④ **日志口径**：日志文件名逐次唯一（含 PID，同秒并发不互撞）且份数可配——
       整份落盘、不许只留 tail。
关键断言：本文件走两条轨。① **文本冻结**（只读脚本原文）：CI 命令表逐字、全量
    参数、固定 venv、日志口径。② **契约实测**（只在参数解析与纯函数上跑 bash）：
    模式标志互斥、`--ci --base` 响亮拒绝、空覆盖退出码 3、范围档选择法。第二条轨
    不建副本、不落日志、不联网；每条用例只跑脚本的参数解析段或一段纯函数。
    "真跑脚本 + 守卫变异"的活体那一半在 `scripts/e2e/dev-verify-e2e.sh`（WSL 内手跑）。
    CI 接线（verify job → 入口脚本 → 门禁脚本）由 tests/test_shared_facts_gate.py
    的 SharedFactsCiWiringTest 钉住，本文件不重复审 ci.yml。
依赖：只读文件 + 本机 bash；无网络、无第三方库。
"""
import io
import os
import re
import shlex
import signal
import subprocess
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRY = os.path.join(BASE, "scripts", "dev-verify.sh")

#: `run_ci` 里必须逐字出现、且按此顺序执行的关键子集命令。
#: `"$py"` = `--ci` 解析出的解释器绝对路径（CI 上是 setup-python 那个）。
EXPECTED_CI_COMMANDS = (
    '"$py" -m ruff check yiban/ tests/ scripts/ web/ --quiet',
    '"$py" -m mypy',
    '"$py" -m pytest tests/test_mypy_type_gate.py -q -p no:randomly',
    '"$py" -m pytest tests/ -q -n 4 --dist loadfile -k '
    '"security or mask or audit or login or private or csrf or ratelimit"',
    '"$py" -m pytest tests/test_login_e2e_mock.py -q -p no:randomly',
    "bash scripts/check-shared-facts.sh",
    '"$py" -m pytest tests/test_shared_facts_gate.py -q -p no:randomly',
    '"$py" scripts/check-path-env-reads.py',
    '"$py" -m pytest tests/test_path_env_read_gate.py -q -p no:randomly',
    '"$py" scripts/check-config-registry.py',
    '"$py" -m pytest tests/test_config_registry_gate.py -q -p no:randomly',
)


def _read(path):
    with io.open(path, encoding="utf-8") as f:
        return f.read()


def _func_body(text, func):
    """切出 bash 脚本里某个函数的正文（列 1 的 `func() {` 起、到列 1 的 `}`）。

    签名行允许带行尾注释（`run_ci() { # …` 是本仓 bash 的常规写法）——不认注释
    会让抽出的正文恒为空串，把三条断言一起打成废断言。
    返回空串表示没抽到——调用方必须判成失败，否则"结构改了"会让断言恒真。
    """
    m = re.search(r"^%s\(\) \{\s*(?:#.*)?$" % re.escape(func), text, re.M)
    if m is None:
        return ""
    rest = text[m.end():]
    end = re.search(r"^\}$", rest, re.M)
    return rest[:end.start()] if end else ""


def _func_source(text, func):
    """返回可直接喂给 bash 的**完整函数定义**（签名行 + 正文 + 闭合括号）。

    `_func_body` 切出的是函数内部语句，单独喂给 `bash -c` 会报
    `local: can only be used in a function`。返回空串表示没抽到。
    """
    body = _func_body(text, func)
    if not body:
        return ""
    return "%s() {\n%s\n}\n" % (func, body)


def _bash_array_items(text, name):
    """抽出 bash 数组 `name=(...)` 的条目（去掉引号，跳过空行与注释行）。

    返回空列表表示没抽到——调用方必须判成失败，否则"结构改了"会让断言恒真。
    """
    m = re.search(r"^%s=\(\s*$" % re.escape(name), text, re.M)
    if m is None:
        return []
    rest = text[m.end():]
    end = re.search(r"^\)\s*$", rest, re.M)
    if end is None:
        return []
    items = []
    for line in rest[:end.start()].splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        items.append(s.strip('"').strip("'"))
    return items


def _pytest_exec_command(text):
    """抽全量/fast 的**真跑** pytest 命令（执行行 + 续行，不含 echo 回显行）。

    只认 `"$PY" -m pytest` 且含 `$TARGET` 的执行行（脚本里唯一的执行点）；续行按行尾
    反斜杠拼接。返回空串表示没抽到——调用方必须判成失败，否则"结构改了"会让断言恒真。
    """
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith('"$PY" -m pytest') and "$TARGET" in ln:
            cmd = ln.strip()
            while cmd.endswith("\\") and i + 1 < len(lines):
                i += 1
                cmd = cmd[:-1].rstrip() + " " + lines[i].strip()
            return cmd
    return ""


def _bash(script, timeout=60):
    """跑一段 bash 脚本，返回 (退出码, stdout+stderr 合并)。不建副本、不联网。"""
    p = subprocess.run(["bash", "-c", script], stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, timeout=timeout, text=True)
    return p.returncode, p.stdout


def _run_entry(args, timeout=60):
    """跑入口脚本本体，返回 (退出码, stdout+stderr 合并)。

    只喂"参数解析阶段就拒绝"的用法，故不建副本、不碰锁、不跑 pytest。超时即杀
    **整个进程组**：脚本在 POSIX 上会把 pytest 拉成孙进程，只杀 bash 会留下孤儿
    进程继续烧 CPU。
    """
    env = dict(os.environ, DEV_VERIFY_ENTRY_TEST_NESTED="1")
    proc = subprocess.Popen(["bash", ENTRY, *args], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, env=env,
                            start_new_session=(os.name == "posix"))
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()
        proc.communicate()
        raise
    return proc.returncode, out


class DevVerifyEntryTest(unittest.TestCase):
    def setUp(self):
        self.text = _read(ENTRY)
        self.ci_body = _func_body(self.text, "run_ci")

    def test_ci_subset_commands_are_frozen_byte_for_byte(self):
        self.assertGreater(len(self.ci_body), 100,
                           "抽不出 run_ci 函数体——结构改了？本条会因此变废断言")
        # 抽"执行行"的口径必须盖住全部三种调用形态：`-m 模块`、`bash 脚本`、
        # `"$py" 脚本路径`（B1 的 AST 门是第三种）。漏一种 = 那一步不进冻结清单。
        got = [line.strip() for line in self.ci_body.splitlines()
               if line.strip().startswith(('"$py" ', "bash scripts/"))]
        self.assertEqual(
            got, list(EXPECTED_CI_COMMANDS),
            "CI 关键子集命令漂了：命令原文只应存在 scripts/dev-verify.sh 一份。"
            "确需改动时，同批改本清单并在 PR 描述里给出改前/改后命令原文")

    def test_full_mode_uses_auto_xdist_with_loadfile(self):
        # 只认**真正执行**的那一行：注释与 echo 里也会出现 `$TARGET`，它们不是跑法。
        # 判据不变——全量的 pytest 执行行只许有一条，且参数逐项冻结。
        lines = [line.strip() for line in self.text.splitlines()
                 if line.lstrip().startswith('"$PY" -m pytest') and "$TARGET" in line]
        self.assertEqual(len(lines), 1, f"全量 pytest 执行行应只有一条，实得 {len(lines)}")
        line = lines[0]
        for token in ("-m pytest", "-q", "-p no:randomly", "-n auto", "--dist loadfile"):
            self.assertIn(token, line, f"全量行缺 {token}：{line}")

    def test_both_guards_are_invoked_in_the_full_mode_path(self):
        """两道守卫必须被**调用**，不只是被定义（在场性）。

        实测（2026-10-05）：只摘掉 `guard_copy_git "$DEST" "$SHA"` 与 `guard_lf "$DEST"`
        两行调用、定义留着，scripts/e2e/dev-verify-e2e.sh 仍全绿（10 次调用 0 FAIL）——
        e2e 的变异体只证"守卫被摘掉后伪红会出现"（必要性），钉不住"守卫还在跑"。
        本条补在场性：调用行消失即红。
        """
        for call in ('guard_copy_git "$DEST" "$SHA"', 'guard_lf "$DEST"'):
            self.assertIn(call, self.text, f"守卫调用行不见了：{call}")

    def test_fixed_venv_is_mandatory_in_full_mode(self):
        self.assertIn("FIXED_VENV=/root/.venv-yiban-wsl", self.text,
                      "固定 venv 路径不许改（解释器漂移是本批动机之一）")
        self.assertIn('PY="$FIXED_VENV/bin/python"', self.text)
        self.assertIn('[ -x "$PY" ] || die "固定 venv 不可用', self.text,
                      "固定 venv 不可用时必须响亮失败，不许退回 PATH 上的 python")

    def test_log_name_is_unique_per_run_and_retention_is_configurable(self):
        m = re.search(r'LOG="\$LOG_DIR/([^"]+)"', self.text)
        self.assertIsNotNone(m, "找不到日志文件名模板")
        name = m.group(1)
        self.assertIn("%Y%m%d-%H%M%S", name, "日志文件名必须带时间戳（保留期按名排序）")
        self.assertIn("$$", name, "日志文件名必须带 PID：同一秒的两次运行不许互相覆盖")
        self.assertIn("prune_logs", self.text, "日志必须按份数轮转（--keep）")
        self.assertIn("DEV-VERIFY summary:", self.text, "日志必须落汇总四数，不许只留 tail")


class DevVerifyFastModeTest(unittest.TestCase):
    """fast 两档的契约（修复单 yiban-auto-sign-rqvx，2026-10-07）。

    钉住六件事：长尾名单不许漂移（文件/类/用例三层都要存在）、副本必须按内容校验、
    空覆盖必须与"真覆盖"可区分、空覆盖提示必须跟着最终退出码、日志目录按模式分开、
    模式标志不许静默后者胜。
    """

    def setUp(self):
        self.text = _read(ENTRY)

    # ---- low#1：FAST_KNOWN_SLOW 防漂移（守卫的元测试）----

    def test_fast_known_slow_entries_are_live_and_not_nested(self):
        entries = _bash_array_items(self.text, "FAST_KNOWN_SLOW")
        self.assertGreaterEqual(len(entries), 1,
                                "抽不到 FAST_KNOWN_SLOW 条目——结构改了？本条会因此变废断言")
        for e in entries:
            path = e.split("::", 1)[0]
            self.assertTrue(
                os.path.isfile(os.path.join(BASE, path)),
                f"FAST_KNOWN_SLOW 条目指向空气：{e}（文件 {path} 不存在）——"
                "文件改名后 --deselect 静默失效，长尾会溜回全量")
        for e in entries:
            for pre in entries:
                if e != pre and e.startswith(pre + "::"):
                    self.fail(f"FAST_KNOWN_SLOW 有前缀嵌套：{e} 已被 {pre} 覆盖，"
                              "长条目是假条目（pytest 的 ::Cls 已含 ::Cls::test_x）")

    def test_fast_known_slow_guard_runs_before_the_copy(self):
        self.assertGreater(len(_func_body(self.text, "guard_fast_known_slow")), 50,
                           "guard_fast_known_slow 未定义或为空：名单没有防漂移守卫")
        m = re.search(r"^\s+guard_fast_known_slow$", self.text, re.M)
        self.assertIsNotNone(m, "guard_fast_known_slow 定义了却没人调用（在场性）")
        self.assertLess(m.start(), self.text.index('"$REPO/" "$DEST/"'),
                        "守卫必须在建副本（rsync）之前跑：名单坏了就要 fail fast")

    def test_fast_known_slow_guard_catches_renamed_class_and_test(self):
        """守卫必须拦"改名后静默变 no-op"：文件在、类或用例改名，剔除即失效。

        pytest 对不存在的 `--deselect` **静默忽略**（审查实测 rc=0、无告警），
        故只查文件存在挡不住"类/方法改名"。这里在临时目录上真跑守卫函数：
        文本匹配类名与用例名，模块级 nodeid（无类名）按形状分支放行，不造假失败。
        """
        src = _func_source(self.text, "guard_fast_known_slow")
        self.assertGreater(len(src), 100, "抽不出 guard_fast_known_slow——结构改了？")
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "tests"))
            mod = os.path.join(d, "tests", "test_x.py")

            def run(entries, content):
                with io.open(mod, "w", encoding="utf-8") as f:
                    f.write(content)
                arr = " ".join('"%s"' % e for e in entries)
                script = ("REPO=%s\nFAST_KNOWN_SLOW=(%s)\n"
                          "die() { echo \"环境错误：$*\" >&2; exit 2; }\n"
                          "%s\nguard_fast_known_slow\n"
                          % (shlex.quote(d), arr, src))
                return _bash(script)

            live = "class CliExitCodeTest:\n    def test_locked(self):\n        pass\n"
            rc, out = run(["tests/test_x.py::CliExitCodeTest::test_locked"], live)
            self.assertEqual(rc, 0, f"名字都在却判红（假失败）：{out}")
            rc, out = run(["tests/test_x.py::CliExitCodeTest::test_locked"],
                          "class CliExitCodeTestRENAMED:\n    def test_locked(self):\n        pass\n")
            self.assertEqual(rc, 2, f"类改名没被拦：剔除静默失效。{out}")
            self.assertIn("CliExitCodeTest", out, "拒绝信息必须点名出问题的条目")
            rc, out = run(["tests/test_x.py::CliExitCodeTest::test_locked"],
                          "class CliExitCodeTest:\n    def test_locked_RENAMED(self):\n        pass\n")
            self.assertEqual(rc, 2, f"用例改名没被拦：剔除静默失效。{out}")
            rc, out = run(["tests/test_x.py::test_module_level"],
                          "def test_module_level():\n    pass\n")
            self.assertEqual(rc, 0, f"模块级 nodeid（无类名）被误判：{out}")
            # low#5：单段 `::` 还有第二种合法形状——类级 nodeid `x.py::Cls`（pytest
            # --deselect 接受 file::Class 整类形状）。名字是真类名时不许判红。
            rc, out = run(["tests/test_x.py::CliExitCodeTest"], live)
            self.assertEqual(rc, 0, f"类级 nodeid（file::Class）被误判成不存在：{out}")
            rc, out = run(["tests/test_x.py::NoSuchClass"], live)
            self.assertEqual(rc, 2, f"假类名（既非类也非用例）没被拦：{out}")
            rc, out = run(["tests/test_missing.py::Cls::t"], live)
            self.assertEqual(rc, 2, f"文件不存在没被拦：{out}")

    def test_fast_known_slow_guard_rejects_prefix_nested_entries(self):
        """守卫必须拦前缀嵌套条目：短条目已覆盖长条目，长条目是假条目。

        复审突变 M5（2026-10-08）：删掉 guard_fast_known_slow 里的嵌套双层循环后，
        入口测试原 18 套仍全绿——嵌套分支没有测试钉。本条补钉：注入
        `file::Cls` 与 `file::Cls::test_x` 前缀嵌套对，守卫必须判红（rc=2）。
        判据：pytest 的 `file::Cls` 已含整类，`file::Cls::test_x` 是多余条目。
        """
        src = _func_source(self.text, "guard_fast_known_slow")
        self.assertGreater(len(src), 100, "抽不出 guard_fast_known_slow——结构改了？")
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "tests"))
            mod = os.path.join(d, "tests", "test_x.py")
            with io.open(mod, "w", encoding="utf-8") as f:
                f.write("class Cls:\n    def test_x(self):\n        pass\n")
            entries = ["tests/test_x.py::Cls", "tests/test_x.py::Cls::test_x"]
            arr = " ".join('"%s"' % e for e in entries)
            script = ("REPO=%s\nFAST_KNOWN_SLOW=(%s)\n"
                      "die() { echo \"环境错误：$*\" >&2; exit 2; }\n"
                      "%s\nguard_fast_known_slow\n"
                      % (shlex.quote(d), arr, src))
            rc, out = _bash(script)
        self.assertEqual(rc, 2, f"前缀嵌套对没被拦（长条目是假条目）：guard rc={rc}，{out}")
        self.assertIn("前缀嵌套", out, "拒绝信息必须点名前缀嵌套")
        self.assertIn("tests/test_x.py::Cls::test_x", out, "拒绝信息必须点名多余的长条目")

    # ---- F1：副本必须按内容校验 ----

    def test_copy_verifies_content_by_checksum(self):
        lines = [ln for ln in self.text.splitlines() if ln.startswith("rsync -")]
        self.assertEqual(len(lines), 1, f"rsync 复制行应只有一条，实得 {len(lines)}")
        tokens = lines[0].split()
        self.assertIn("-c", tokens,
                      "rsync 必须带 -c：默认快检只看尺寸+整秒 mtime，"
                      "同尺寸同秒改动会被跳过，副本陈旧即静默假绿")
        self.assertIn("--delete", tokens, "副本必须 --delete：源里删掉的文件副本里也要没")

    # ---- M3：--deselect 拼接必须真进到跑测命令里 ----

    def test_fast_pytest_command_splices_deselect_flags(self):
        """fast 的真跑命令必须把 DESELECT 数组拼进去（M3）。

        审查实测（2026-10-07）：把真跑命令里的 `${DESELECT[@]+"${DESELECT[@]}"}` 整段
        删掉后，入口测试 16 套全绿 rc=0——上一行 echo 仍宣称"已剔除 5 条已知长尾"，
        而真跑命令里一条 `--deselect` 也没有。5 条长尾因此全部溜回 --fast，
        自查墙钟从 57s 回到 146s，且绿色是假象。本条钉住"拼接承重"：抽真跑命令
        （执行行 + 续行，不含 echo 回显）断言数组拼接在场。
        突变验证：删掉该段 ⇒ 本条红（见 work/m3-mutant.log）。
        """
        cmd = _pytest_exec_command(self.text)
        self.assertGreater(len(cmd), 50, "抽不出真跑 pytest 命令——结构改了？本条会因此变废断言")
        self.assertRegex(
            cmd, r"\$\{DESELECT\[@\]",
            "真跑 pytest 命令没拼 DESELECT 数组：--deselect 不会生效，长尾静默溜回 --fast。"
            "只留 echo 回显不算——回显不是跑测命令")
        build = [ln for ln in self.text.splitlines()
                 if "DESELECT+=(" in ln and "--deselect" in ln]
        self.assertEqual(len(build), 1,
                         "DESELECT 数组必须由 FAST_KNOWN_SLOW 逐条装配 --deselect（装配点应只有一处）")

    # ---- F4：空覆盖必须与"真覆盖"可区分 ----

    def test_scoped_selection_marks_empty_coverage(self):
        src = _func_source(self.text, "select_fast_targets")
        self.assertGreater(len(src), 100, "抽不出 select_fast_targets——结构改了？")
        with tempfile.TemporaryDirectory() as d:
            empty = os.path.join(d, "changed.txt")
            io.open(empty, "w", encoding="utf-8").close()
            flag = os.path.join(d, "flag.txt")
            script = ("REPO=%s\n%s\nselect_fast_targets %s %s\n"
                      % (shlex.quote(BASE), src, shlex.quote(empty), shlex.quote(flag)))
            rc, out = _bash(script)
            self.assertEqual(rc, 0, f"空改动集跑 select_fast_targets 失败：{out}")
            self.assertEqual(out.split()[-1], "tests/test_dev_verify_entry.py",
                             "空改动集只跑入口自检")
            self.assertEqual(_read(flag).strip(), "1",
                             "空改动集必须写空覆盖标记：否则调用方把'只跑了入口自检'当'已覆盖'")
            real = os.path.join(d, "changed2.txt")
            with io.open(real, "w", encoding="utf-8") as f:
                f.write("tests/test_dev_verify_entry.py\n")
            flag2 = os.path.join(d, "flag2.txt")
            script2 = ("REPO=%s\n%s\nselect_fast_targets %s %s\n"
                       % (shlex.quote(BASE), src, shlex.quote(real), shlex.quote(flag2)))
            rc2, out2 = _bash(script2)
            self.assertEqual(rc2, 0, f"有改动集跑 select_fast_targets 失败：{out2}")
            self.assertEqual(_read(flag2).strip(), "0",
                             "有命中用例时不许写空覆盖标记（否则正常跑也退 3）")

    def test_empty_coverage_maps_to_exit_code_three(self):
        src = _func_source(self.text, "resolve_exit_code")
        self.assertGreater(len(src), 30,
                           "resolve_exit_code 未定义：退出码没有单一裁决点，空覆盖无法与失败区分")
        # (ruff_rc, pytest_rc, 空覆盖) → 期望退出码。最终契约只有四种码：
        # 0 通过 / 1 有红 / 2 环境错误 / 3 空覆盖。ruff 与 pytest 的任何非 0 一律归一为 1，
        # 原始码只留在日志里（ruff_exit= / pytest_exit=）。3 由"空覆盖"独占，
        # 故 pytest 自己的 3（INTERNALERROR）不许与空覆盖撞码。
        cases = [((0, 0, 0), "0"), ((0, 0, 1), "3"), ((1, 0, 0), "1"),
                 ((0, 2, 0), "1"), ((0, 2, 1), "1"), ((1, 0, 1), "1"),
                 ((0, 3, 0), "1"), ((0, 5, 0), "1"), ((1, 3, 0), "1")]
        for (ruff_rc, py_rc, empty), want in cases:
            rc, out = _bash("%s\nresolve_exit_code %d %d %d\n" % (src, ruff_rc, py_rc, empty))
            self.assertEqual((rc, out.strip()), (0, want),
                             f"final rc 不对：ruff={ruff_rc} pytest={py_rc} 空覆盖={empty} → {out.strip()}")
        self.assertIn('resolve_exit_code "$lint_rc" "$py_rc" "${FAST_EMPTY:-0}"', self.text,
                      "调用点必须把空覆盖标记传进裁决点；第一参是 ruff+mypy 归一后的静态检查码")
        self.assertIn("DEV-VERIFY(fast): covered=", self.text,
                      "fast 档汇总行必须打覆盖数：只跑入口自检与真覆盖要能区分")

    def test_raw_lint_and_pytest_codes_stay_visible_in_the_log(self):
        """归一为 1 之后原始码必须仍逐行打印：否则定位信息随归一一起丢失。"""
        self.assertIn('echo "DEV-VERIFY ruff_exit=$ruff_rc"', self.text,
                      "ruff 原始退出码必须进日志")
        self.assertIn('echo "DEV-VERIFY mypy_exit=$mypy_rc"', self.text,
                      "mypy 原始退出码必须进日志")
        self.assertIn('echo "DEV-VERIFY pytest_exit=$py_rc"', self.text,
                      "pytest 原始退出码必须进日志")

    def test_empty_coverage_note_follows_the_final_exit_code(self):
        """空覆盖提示必须按最终 rc 打印：只按 FAST_EMPTY 打会与真红报的码自相矛盾。

        实测（审查场景 B）：提示写"退出码 3"，下一行写"exit_code=1"。
        """
        src = _func_source(self.text, "note_fast_empty_coverage")
        self.assertGreater(len(src), 50, "抽不出 note_fast_empty_coverage——结构改了？")
        rc, out = _bash("%s\nnote_fast_empty_coverage 1 3\n" % src)
        self.assertEqual(rc, 0, f"提示函数跑失败：{out}")
        self.assertIn("空覆盖", out, "空覆盖必须被点名")
        self.assertIn("退出码 3", out, "真正空覆盖时要报 3")
        rc, out = _bash("%s\nnote_fast_empty_coverage 1 1\n" % src)
        self.assertEqual(rc, 0, f"提示函数跑失败：{out}")
        self.assertIn("实际退出码 1", out, "空覆盖同时有红时要报真实码 1")
        self.assertNotIn("退出码 3", out, "有红时不许喊空覆盖的 3")
        rc, out = _bash("%s\nnote_fast_empty_coverage 0 0\n" % src)
        self.assertEqual((rc, out.strip()), (0, ""), "非空覆盖不许打印空覆盖提示")
        m = re.search(r'^\s*note_fast_empty_coverage "\$\{FAST_EMPTY', self.text, re.M)
        self.assertIsNotNone(m, "空覆盖提示没有调用点（在场性）")
        self.assertGreater(m.start(), self.text.index('rc=$(resolve_exit_code'),
                           "提示必须在最终 rc 算出之后打印：只看 FAST_EMPTY 会与真红报的码矛盾")

    # ---- low#4：日志目录必须按模式分开（fast 与全量走不同的锁，可并发）----

    def test_default_log_dir_is_separate_per_mode(self):
        src = _func_source(self.text, "default_log_dir")
        self.assertGreater(len(src), 50, "抽不出 default_log_dir——结构改了？")
        rc, out = _bash("%s\ndefault_log_dir /x/y fast\necho\ndefault_log_dir /x/y full\n" % src)
        self.assertEqual(rc, 0, f"default_log_dir 跑失败：{out}")
        self.assertEqual(out.split(),
                         ["/x/yiban-dev-verify-logs-fast", "/x/yiban-dev-verify-logs"],
                         "fast 与全量的默认日志目录必须不同：两者走不同的锁、可并发，"
                         "共用目录会让 prune_logs 轮转删掉对方的日志")
        self.assertIn('LOG_DIR=$(default_log_dir "$REPO" "$MODE")', self.text,
                      "默认日志目录必须由模式决定（调用点在场性）")

    # ---- low#2 / low#3：参数不许静默忽略 ----

    def test_repeated_same_mode_flag_is_idempotent(self):
        """同一模式标志重复给必须幂等接受（L3）。

        实测（2026-10-07 审查）：`--fast --fast` 被 `mark_mode` 当成"第二个模式标志"
        报"互斥"退出 2。重复同一标志不改变语义，拒绝它只会误伤脚本化调用。
        异名（`--fast --fast-scoped`）仍必须拒绝——那是危险方向，不许一起放开。
        本条只跑纯函数，不拉起入口脚本（避免重跑 fast、触发套娃）。
        """
        src = _func_source(self.text, "mark_mode")
        self.assertGreater(len(src), 50, "抽不出 mark_mode——结构改了？本条会因此变废断言")
        prelude = "MODE_FLAG=\"\"\ndie() { echo \"环境错误：$*\" >&2; exit 2; }\n"
        rc, out = _bash("%s%s\nmark_mode --fast\nmark_mode --fast\nprintf 'OK\\n'\n"
                        % (prelude, src))
        self.assertEqual((rc, out.strip()), (0, "OK"),
                         f"同一模式标志重复给被拒绝（应为幂等接受）：{out}")
        rc, out = _bash("%s%s\nmark_mode --fast\nmark_mode --fast-scoped\nprintf 'OK\\n'\n"
                        % (prelude, src))
        self.assertEqual(rc, 2, f"不同模式标志没被拒绝：{out}")
        self.assertIn("互斥", out, f"拒绝信息必须说清互斥：{out}")

    def test_second_mode_flag_is_rejected_loudly(self):
        if os.environ.get("DEV_VERIFY_ENTRY_TEST_NESTED") == "1":
            # 嵌套运行：本用例会拉起入口脚本，脚本可能再跑到本文件（修复前/变异体）。
            # 不拦住就会无限套娃，把机器 CPU 吃满。
            self.skipTest("嵌套运行：跳过拉起入口脚本的用例")
        try:
            # 拒绝发生在参数解析段，正常不到 1 秒；给 20 秒是给慢机器留余量，
            # 同时把"没有拒绝、继续跑下去"这种情况快速判红。
            rc, out = _run_entry(["--repo", BASE, "--fast", "--fast-scoped"], timeout=20)
        except subprocess.TimeoutExpired:
            self.fail("脚本没有拒绝两个模式标志：它继续跑了下去（20 秒未退出）")
        self.assertNotEqual(rc, 0, "两个模式标志被静默接受（后者胜）：--fast --fast-scoped 会退成范围档")
        self.assertIn("互斥", out, f"拒绝信息必须说清互斥：{out[-400:]}")

    @unittest.skipUnless(os.name == "posix",
                         "--ci 在 Windows 侧被上游拒绝（信息不同），只在 POSIX 侧验证")
    def test_ci_mode_rejects_base_loudly(self):
        if os.environ.get("DEV_VERIFY_ENTRY_TEST_NESTED") == "1":
            self.skipTest("嵌套运行：跳过拉起入口脚本的用例")
        rc, out = _run_entry(["--repo", BASE, "--ci", "--base", "HEAD"], timeout=30)
        self.assertNotEqual(rc, 0, "--ci 静默忽略了 --base")
        self.assertIn("--base", out, f"拒绝信息必须点名 --base：{out[-400:]}")


if __name__ == "__main__":
    unittest.main()
