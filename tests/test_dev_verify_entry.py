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
       `"$py" 脚本路径`（B1 的 AST 门用第三种）。认少一种，那一步就不进冻结清单，
       命令原文就此多出第二份事实源。
    ② **全量参数固定**：默认模式 `-n auto --dist loadfile`。`--dist loadfile`
       不得去掉：套内存在文件内先后依赖与进程级 DB 单例，按单条分发即误红。
    ③ **固定 venv**：全量模式只认 `/root/.venv-yiban-wsl/bin/python`，不可用就
       响亮失败，不许退化成 PATH 上的任意 python。
    ④ **日志口径**：日志文件名逐次唯一（含 PID，同秒并发不互撞）且份数可配——
       整份落盘、不许只留 tail。
关键断言：本文件是**文本冻结**测试（只读文件、不执行 bash）；"真跑脚本 + 守卫
    变异"的活体那一半在 `scripts/e2e/dev-verify-e2e.sh`（WSL 内手跑）。
    CI 接线（verify job → 入口脚本 → 门禁脚本）由 tests/test_shared_facts_gate.py
    的 SharedFactsCiWiringTest 钉住，本文件不重复审 ci.yml。
依赖：只读文件；无 bash、无网络、无第三方库。
"""
import io
import os
import re
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRY = os.path.join(BASE, "scripts", "dev-verify.sh")

#: `run_ci` 里必须逐字出现、且按此顺序执行的关键子集命令。
#: `"$py"` = `--ci` 解析出的解释器绝对路径（CI 上是 setup-python 那个）。
EXPECTED_CI_COMMANDS = (
    '"$py" -m ruff check yiban/ tests/ scripts/ web/ --quiet',
    '"$py" -m pytest tests/ -q -n 4 --dist loadfile -k '
    '"security or mask or audit or login or private or csrf or ratelimit"',
    '"$py" -m pytest tests/test_login_e2e_mock.py -q -p no:randomly',
    "bash scripts/check-shared-facts.sh",
    '"$py" -m pytest tests/test_shared_facts_gate.py -q -p no:randomly',
    '"$py" scripts/check-path-env-reads.py',
    '"$py" -m pytest tests/test_path_env_read_gate.py -q -p no:randomly',
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


if __name__ == "__main__":
    unittest.main()
