# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""兼容壳：`yiban.engine.workers` → `yiban.workers.workers`（仓库结构整理批 B）。

**为什么有本件**
workers（多执行体监督进程与兜底常驻执行体）在批 B 搬进目标层
（目标架构契约 `docs/dev/architecture-target.md` 第 3 节：`engine/workers.py` → `workers/`）。
读者面里有 `docker/**`、`scripts/**`、`tests/**`。`web/app.py` 的再导出墙与测试面的
旧坐标读者本批**不动**（那是另一笔账）。故旧坐标留转发壳。

**本件不是第二份实现**
本件把 `sys.modules` 里本坐标的条目替换为 `yiban.workers.workers` 的**同一模块对象**。
故属性访问（`yiban.engine.workers.X`）与 `mock.patch("yiban.engine.workers.X")` 都落到
实现的那一份，不会静默打空。判据是**对象同一**，验证者＝`tests/test_batch_b_relocation.py`。

**为什么用 `sys.modules` 替换，不用 `__getattr__` 转发**
`__getattr__` 转发会造出第二个模块对象；`mock.patch("yiban.engine.workers.X")` 只改壳的属性，
不改实现被真调用的那份——那正是本仓实证过的"搬家后打桩静默失效"形状。
替换 `sys.modules` 条目后旧坐标**就是**实现模块本身。

**已知的层归属张力（如实登记）**
本模块一半是**编排**（`--workers N` 的派发与池）。契约第 1 节的 APP 子图把"协调 · 派发与
工作体池"画在应用层，第 3 节目标树写 `app/coord.py # 派发与工作体池（现 workers）`。
本批按第 3 节**映射表**的"移到 `workers/`"整文件搬入。把编排面拆去 `app/coord.py`
属第三刀（与 `executor_v3` 的拆分同批）。本批不改行为，故不拆。

**退役条件**
读者面（`docker/**`、`scripts/**`、`tests/**`）改到新坐标后删除本件。
工单见本批交付报告。
"""
import sys as _sys

import yiban.workers.workers as _impl

_sys.modules[__name__] = _impl
