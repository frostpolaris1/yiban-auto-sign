# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""兼容壳：`yiban.cli` → `yiban.entry.cli`（仓库结构整理批 A1）。

**为什么有本件**
命令行入口在批 A1 搬进入口层（目标架构契约 `docs/dev/architecture-target.md`
第 3 节）。契约文档 `docs/dev/cli.md` 记载的调用形态是 `python -m yiban.cli`。
读者面还有 `scripts/deploy_gate.sh`（`from yiban import cli`）与 `tests/`。
一次改完这些读者＝把坐标迁移与行为改动搅在同一批 diff 里。故旧坐标留转发壳。

**本件不是第二份实现**
本件不复制任何判据。它把属性访问转发给 `yiban.entry.cli` 的同一对象。
判据是**对象同一**（`yiban.cli.main is yiban.entry.cli.main`），
验证者＝`tests/test_entry_cli_relocation.py`。

**转发为什么用模块级 `__getattr__`，不写显式名单**
显式名单漏一个名字，那个读者静默换成 `AttributeError`。
`__getattr__` 按构造完整：新实现的每个名字都取得到。
代价：`mock.patch("yiban.cli.X")` 只改本壳的属性，不改实现的模块全局。
故**打桩目标必须写新坐标**（`yiban.entry.cli.X`）。这是本批 A5 的迁移面。

**退役条件**
读者面（`docs/dev/cli.md`、`scripts/deploy_gate.sh`、`tests/`）改到新坐标后，
删除本件。工单见本批交付报告。
"""
import sys

from yiban.entry import cli as _impl


def __getattr__(name):
    """把属性访问转发给实现模块（同一个对象）。"""
    try:
        return getattr(_impl, name)
    except AttributeError:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
            f"（本件是转发壳，实现里也没有这个名字）") from None


def __dir__():
    """`dir(yiban.cli)` 与 `dir(yiban.entry.cli)` 同面。"""
    return sorted(set(globals()) | set(dir(_impl)))


if __name__ == "__main__":
    sys.exit(_impl.main(sys.argv[1:]))
