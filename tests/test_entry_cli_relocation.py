# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""命令行入口搬家后的等价性守卫（仓库结构整理批 A1）。

标签：M · 架构分层与基础设施
覆盖：本批把命令行入口从 `yiban/cli.py` 搬到 `yiban/entry/cli/`
    （目标架构契约 `docs/dev/architecture-target.md` 第 3 节）。旧路径留转发壳。
    本文件钉两条不变式：
    ① **两个入口等价**：`python -m yiban.cli` 与 `python -m yiban.entry.cli`
       对同一子命令给出同一退出码与同一 stdout。
    ② **只有一份实现**：旧路径是转发壳，不是第二份实现。判据是**对象同一**
       （`is`），不是"输出看着一样"。
对应实现：`yiban/entry/cli/__init__.py`（实现）、`yiban/entry/cli/__main__.py`
    （`-m` 入口）、`yiban/cli.py`（兼容壳）。
关键断言：① 起真子进程比退出码与 stdout（进程级契约，不是 `main()` 的返回值）；
    ② 在进程内比对象同一。
依赖：起 `sys.executable -m <模块>` 子进程（纯 Python，不联网、不碰本机 .env）。
    隔离环境夹具复用 `tests/test_cli_contract.py::_cli_env`——那是"CLI 隔离环境"
    的唯一事实源，本文件不再抄第二份。
已知边界：本文件**不**重测 CLI 的行为本身（退出码表、stdout/stderr 分流、默认不动手）。
    那是 `tests/test_cli_contract.py` 的职责；它经旧路径起子进程，故同时钉住新实现。
    ② 只在**进程内**判对象同一：子进程里判不到对象身份。
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_cli_contract import BASE, _cli_env  # 夹具唯一事实源

#: 新坐标（目标树）与旧坐标（兼容壳）。
NEW_MODULE = "yiban.entry.cli"
OLD_MODULE = "yiban.cli"

#: 子命令全集（`docs/dev/cli.md` 一节的表：八个）。
SUBCOMMANDS = ("sign", "probe", "config", "capacity", "state", "egress", "db", "version")

#: 旧路径必须转发**同一对象**的名字。
#: 这批名字＝`docs/dev/cli.md` 的读者面（`scripts/deploy_gate.sh` 用 `cli.main`）
#: 加 `tests/` 里已出现的用法（`_say` / `_emit_json` / `_cmd_sign` / `_PASSTHROUGH` /
#: `_capacity_numbers` / `store_db`）。漏一个，那个读者静默换成 AttributeError。
FORWARDED_NAMES = (
    "main", "USAGE", "_PASSTHROUGH",
    "_say", "_emit_json", "_emit_json_error", "_fail", "_exit_code", "_guess_command",
    "_env_view", "_paths", "_cfg_value", "_cfg_int", "_capacity_numbers",
    "_cmd_sign", "_cmd_probe", "_cmd_config", "_cmd_capacity", "_cmd_state",
    "_cmd_version", "_build_parser", "_dispatch", "_fail_usage",
    "store_db", "purge_guard", "cli_support", "runner", "state_gc", "window",
    "env_io", "mask_phone", "RELEASE_VERSION",
)


def _run(module, argv, env, timeout=180):
    """起一次 `-m <module>` 子进程（cwd=仓库根，stdin 关掉）。"""
    return subprocess.run([sys.executable, "-m", module, *argv], cwd=BASE, env=env,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", stdin=subprocess.DEVNULL, timeout=timeout)


class EntryPointEquivalenceTest(unittest.TestCase):
    """① 两个入口对同一子命令等价（退出码与 stdout）。"""

    def _compare(self, argv):
        """同一隔离环境里先后跑两个入口，返回 (旧, 新) 两次结果。

        同一环境（同一个临时目录）是刻意的：本仓 CLI 的只读子命令重跑幂等，
        故"先后跑"不会把状态差异读成行为差异。
        """
        with tempfile.TemporaryDirectory() as tmp:
            env = _cli_env(Path(tmp))
            old = _run(OLD_MODULE, argv, env)
            new = _run(NEW_MODULE, argv, env)
        return old, new

    def test_every_subcommand_agrees_on_rc_and_stdout(self):
        """八个子命令逐条对拍：退出码与 stdout 必须逐字节相同。"""
        for cmd in SUBCOMMANDS:
            for extra in (["--json"], []):
                argv = [cmd, *extra]
                with self.subTest(argv=argv):
                    old, new = self._compare(argv)
                    self.assertEqual(
                        new.returncode, old.returncode,
                        f"{argv}：新旧入口退出码不同（旧 {old.returncode} / 新 {new.returncode}）"
                        f"\n旧 stderr={old.stderr!r}\n新 stderr={new.stderr!r}")
                    self.assertEqual(
                        new.stdout, old.stdout,
                        f"{argv}：新旧入口 stdout 不同\n旧={old.stdout!r}\n新={new.stdout!r}")

    def test_usage_surface_agrees(self):
        """用法面（无参数、`--help`、未知子命令、未知开关）逐条对拍。"""
        for argv in ([], ["--help"], ["nosuchcmd", "--json"], ["db", "--nosuchflag"]):
            with self.subTest(argv=argv):
                old, new = self._compare(argv)
                self.assertEqual(new.returncode, old.returncode,
                                 f"{argv}：退出码不同（旧 {old.returncode} / 新 {new.returncode}）")
                self.assertEqual(new.stdout, old.stdout,
                                 f"{argv}：stdout 不同\n旧={old.stdout!r}\n新={new.stdout!r}")


class SingleImplementationTest(unittest.TestCase):
    """② 旧路径是转发壳：转发的是同一对象，不是第二份实现。"""

    def test_shell_forwards_the_same_objects(self):
        from yiban import cli as old
        from yiban.entry import cli as new

        self.assertIsNot(old, new, "旧路径与新路径必须是两个模块对象（壳与实现）")
        for name in FORWARDED_NAMES:
            with self.subTest(name=name):
                self.assertTrue(hasattr(new, name), f"新实现缺名字 {name}")
                self.assertIs(getattr(old, name), getattr(new, name),
                              f"旧路径的 {name} 不是新实现的同一对象："
                              "那是第二份实现（改一处不会同步另一处）")

    def test_new_module_has_a_main_entry(self):
        """`python -m yiban.entry.cli` 必须有入口件：包目录要有 `__main__.py`。"""
        import os

        from yiban.entry import cli as new

        pkg = os.path.dirname(new.__file__)
        self.assertTrue(os.path.isfile(os.path.join(pkg, "__main__.py")),
                        "缺 yiban/entry/cli/__main__.py：`-m yiban.entry.cli` 跑不起来")


if __name__ == "__main__":
    unittest.main(verbosity=2)
