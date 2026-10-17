# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""兼容壳：`yiban.window` → `yiban.core.window`（仓库结构整理批 B）。

**为什么有本件**
window（签到窗口判定）在批 B 搬进目标层（目标架构契约 `docs/dev/architecture-target.md`
第 3 节）。读者面里有 `web/**`、`scripts/**`、`tests/**`。`web/app.py` 的再导出墙
与测试面本批**不动**（那是另一笔账）。故旧坐标留转发壳。

**本件不是第二份实现**
本件把 `sys.modules` 里本坐标的条目替换为 `yiban.core.window` 的**同一模块对象**。
故属性访问（`yiban.window.X`）与 `mock.patch("yiban.window.X")` 都落到实现的那一份，不会静默打空。
判据是**对象同一**，验证者＝`tests/test_batch_b_relocation.py`。

**为什么用 `sys.modules` 替换，不用 `__getattr__` 转发**
`__getattr__` 转发会造出第二个模块对象；`mock.patch("yiban.window.X")` 只改壳的属性，
不改实现被真调用的那份——那正是本仓实证过的"搬家后打桩静默失效"形状。
替换 `sys.modules` 条目后旧坐标**就是**实现模块本身。

**退役条件**
读者面（`web/**`、`scripts/**`、`tests/**`）改到新坐标后删除本件。
工单见本批交付报告。
"""
import sys as _sys

import yiban.core.window as _impl

_sys.modules[__name__] = _impl
