# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""兼容壳：`yiban.store` → `yiban.adapters.persistence`（仓库结构整理批 B）。

**为什么有本件**
数据访问层在批 B 整包搬进适配器层（目标架构契约 `docs/dev/architecture-target.md`
第 3 节）。读者面里有 `web/**`、`scripts/**`、`tests/**`。故旧坐标留转发壳。

**本件不是第二份实现**
各子模块在 `sys.modules` 里登记为实现的**同一模块对象**，且属性访问转发给实现包。
故 `from yiban.store import db`、`import yiban.store.db` 与
`mock.patch("yiban.store.db.X")` 都落到实现的那一份。
判据是**对象同一**，验证者＝`tests/test_batch_b_relocation.py`。

**子模块为什么登记进 `sys.modules`**
读者面里有 `from yiban.store import db`、`import yiban.store.db` 与
`mock.patch("yiban.store.db.X")` 三种写法。登记同一对象后三者都落到位。

**退役条件**
读者面（`web/**`、`scripts/**`、`tests/**`）改到新坐标后删除本件。
工单见本批交付报告。
"""
import importlib as _importlib
import sys as _sys

from yiban.adapters import persistence as _impl

_SUBMODULES = (
    "accounts",
    "audit_chain",
    "claims",
    "cleanup",
    "clock_meta",
    "connection",
    "db",
    "events",
    "migrations",
    "purge_guard",
    "queue_store",
    "run_events",
    "session_cache",
    "time_prefs",
    "tracking",
    "users",
    "verify_jobs",
)

for _name in _SUBMODULES:
    _sys.modules[__name__ + "." + _name] = _importlib.import_module(
        "yiban.adapters.persistence." + _name)


def __getattr__(name):
    """把属性访问转发给实现包（同一个对象）。"""
    try:
        return getattr(_impl, name)
    except AttributeError:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
            f"（本件是转发壳，实现里也没有这个名字）") from None


def __dir__():
    """`dir()` 覆盖实现包的模块名，外加本壳自己的私有名。"""
    return sorted(set(globals()) | set(dir(_impl)))
