# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""`python -m yiban.cli <子命令> [选项]`：agent 侧的统一入口（契约见 `docs/dev/cli.md`）。

本步只先接 `sign` 子命令——它把去掉子命令名之后的参数原样转交 `yiban.engine.runner`，
并把 runner **返回**的退出码当作进程退出码（语义见 `docs/dev/cli.md` §3）。其余子命令
（`probe` / `config` / `capacity` / `state` / `db` / `version`）与 `--json` 尚未实施。

两条硬性约定在这里落地：**不交互**（不认识子命令就打一行用法到 stderr 并返回 2，
绝不读 stdin）、**stdout 放结果**（人类可读的日志由引擎写往 stderr/日志文件）。
"""
import sys

from yiban.engine import runner

USAGE = "用法: python -m yiban.cli sign [选项]"


def main(argv=None) -> int:
    """执行子命令并返回退出码（调用方决定是否 `sys.exit`）。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] != "sign":
        print(USAGE, file=sys.stderr)
        return 2
    return runner.main(argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
